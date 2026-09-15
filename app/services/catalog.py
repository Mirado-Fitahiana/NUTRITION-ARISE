"""Recalcul et règles de publication du catalogue (D-11, FN-003, FN-008, FN-009).

`app.services.nutrition` porte les calculs purs ; ce module fait le pont avec
les entités SQLAlchemy. Il existe pour une raison précise : **le recalcul ne
doit avoir qu'une seule implémentation.** Le chargeur de seed, l'API
d'administration et le banc d'essai doivent produire exactement les mêmes
valeurs, sinon « recalculer » ne veut plus rien dire.

Il porte aussi le contrôle de publication de FN-008, dont l'ordre des refus
compte : on répond d'abord « ce plat n'a pas d'ingrédient », pas
« allergènes non vérifiés » — le second message serait vrai mais inutile.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.catalog import Dish, DishAllergen, DishTagLink, Ingredient
from app.models.enums import Allergen, DishStatus, ReferenceUnit, RestrictionFlag
from app.services.nutrition import (
    DerivedDishData,
    DishComponent,
    IngredientFacts,
    derive_dish_data,
)
from app.services.units import IngredientConversion


def facts_from_row(row: Ingredient) -> IngredientFacts:
    """Ingrédient en base vers vue de calcul.

    Les colonnes tableau sont stockées en `text[]` borné par contrainte ; elles
    sont retypées ici pour que le calcul manipule des énumérations.
    """
    return IngredientFacts(
        slug=row.slug,
        reference_unit=ReferenceUnit(row.reference_unit),
        kcal_100=Decimal(row.kcal_100),
        protein_100=Decimal(row.protein_100),
        carbs_100=Decimal(row.carbs_100),
        fat_100=Decimal(row.fat_100),
        fiber_100=Decimal(row.fiber_100),
        allergens=frozenset(Allergen(a) for a in row.allergens),
        restriction_flags=frozenset(RestrictionFlag(f) for f in row.restriction_flags),
        density_g_per_ml=(
            Decimal(row.density_g_per_ml) if row.density_g_per_ml is not None else None
        ),
        conversions=tuple(
            IngredientConversion(c.from_unit, c.to_unit, Decimal(c.factor))
            for c in row.unit_conversions
        ),
        is_allergen_verified=row.is_allergen_verified,
    )


def components_from_dish(dish: Dish) -> list[DishComponent]:
    """Composition d'un plat, dans l'ordre d'affichage saisi."""
    return [
        DishComponent(
            ingredient=facts_from_row(link.ingredient),
            quantity=Decimal(link.quantity),
            unit=link.unit,
        )
        for link in sorted(dish.ingredients, key=lambda i: i.display_order)
    ]


def recompute(dish: Dish) -> DerivedDishData:
    """Recalcule tout ce que le plat tient de ses ingrédients, sans écrire.

    Lève `UnitConversionError` si une conversion manque : le refus doit
    remonter jusqu'à l'appelant, jamais être absorbé (FN-012).
    """
    return derive_dish_data(components_from_dish(dish), dish.servings)


def apply_derived(dish: Dish, derived: DerivedDishData) -> None:
    """Écrit les valeurs dérivées sur le plat.

    Les tags saisis à la main sont conservés ; seuls les tags dérivés sont
    remplacés. Les allergènes ajoutés manuellement le sont aussi : FN-008
    autorise un administrateur à *ajouter* un allergène, jamais à retirer un
    allergène propagé.
    """
    dish.kcal_portion = derived.nutrition.kcal
    dish.protein_portion = derived.nutrition.protein_g
    dish.carbs_portion = derived.nutrition.carbs_g
    dish.fat_portion = derived.nutrition.fat_g
    dish.fiber_portion = derived.nutrition.fiber_g
    dish.nutrition_computed_at = datetime.now(UTC)

    dish.compatible_restrictions = sorted(
        restriction.value for restriction in derived.compatible_restrictions
    )

    manual_tags = [link for link in dish.tags if not link.is_derived]
    manual_values = {link.tag for link in manual_tags}
    dish.tags.clear()
    dish.tags.extend(manual_tags)
    dish.tags.extend(
        DishTagLink(tag=tag, is_derived=True)
        for tag in sorted(derived.derived_tags, key=lambda t: t.value)
        if tag not in manual_values
    )

    manual_allergens = [link for link in dish.allergens if link.is_manual]
    manual_allergen_values = {link.allergen for link in manual_allergens}
    dish.allergens.clear()
    dish.allergens.extend(manual_allergens)
    dish.allergens.extend(
        DishAllergen(allergen=allergen, is_manual=False)
        for allergen in sorted(derived.allergens, key=lambda a: a.value)
        if allergen not in manual_allergen_values
    )


def publication_blockers(dish: Dish, derived: DerivedDishData | None = None) -> list[str]:
    """FN-008 — ce qui empêche ce plat d'être publié, en clair.

    Renvoie une liste vide quand la publication est possible. L'ordre suit
    celui dans lequel un rédacteur les rencontrerait.
    """
    blockers: list[str] = []

    if not dish.ingredients:
        blockers.append("le plat ne contient aucun ingrédient")
        return blockers

    if not dish.meal_types:
        blockers.append("aucun type de repas n'est renseigné")

    if dish.validated_by is None or dish.validated_at is None:
        blockers.append("le plat n'a pas été validé par un nutritionniste")
    elif dish.author_id is not None and dish.validated_by == dish.author_id:
        blockers.append("le validateur ne peut pas être l'auteur du plat")

    if derived is None:
        derived = recompute(dish)

    if derived.unverified_ingredients:
        # FN-003 : le blocage massif au démarrage est voulu.
        blockers.append(
            "allergènes non vérifiés pour : "
            + ", ".join(sorted(derived.unverified_ingredients))
        )

    if dish.nutrition_computed_at is None:
        blockers.append("les valeurs nutritionnelles n'ont jamais été calculées")

    return blockers


#: Transitions autorisées du cycle de vie (FN-008). Toute transition absente de
#: cette table est refusée : c'est la table qui fait foi, pas une suite de `if`.
ALLOWED_TRANSITIONS: dict[DishStatus, frozenset[DishStatus]] = {
    DishStatus.DRAFT: frozenset({DishStatus.PENDING_VALIDATION, DishStatus.ARCHIVED}),
    DishStatus.PENDING_VALIDATION: frozenset(
        {DishStatus.VALIDATED, DishStatus.REJECTED, DishStatus.DRAFT}
    ),
    DishStatus.VALIDATED: frozenset(
        {DishStatus.PUBLISHED, DishStatus.DRAFT, DishStatus.ARCHIVED}
    ),
    DishStatus.PUBLISHED: frozenset({DishStatus.ARCHIVED}),
    DishStatus.REJECTED: frozenset({DishStatus.DRAFT, DishStatus.ARCHIVED}),
    # Un plat archivé y reste : il a pu être recommandé, et l'historique des
    # programmes s'appuie sur le figement, pas sur une résurrection.
    DishStatus.ARCHIVED: frozenset(),
}


def can_transition(current: DishStatus, target: DishStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


__all__ = [
    "ALLOWED_TRANSITIONS",
    "apply_derived",
    "can_transition",
    "components_from_dish",
    "facts_from_row",
    "publication_blockers",
    "recompute",
]
