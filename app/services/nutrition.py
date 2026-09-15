"""Calculs dérivés du catalogue (D-11, FN-003, FN-008, FN-009).

Ce module ne touche pas la base : il opère sur des structures immuables, ce qui
le rend testable sans PostgreSQL. C'est délibéré — ce sont les règles les plus
critiques du module, celles que la suite de tests §14.1 doit couvrir.

Trois dérivations, toutes recalculées à chaque modification :

* **valeurs nutritionnelles** — calculées depuis les ingrédients, jamais saisies ;
* **allergènes** — union de ceux des ingrédients, jamais réduite à la main ;
* **tags et compatibilités** — déduits des drapeaux de restriction.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from app.models.enums import (
    Allergen,
    DishTag,
    ReferenceUnit,
    RestrictionFlag,
    RestrictionType,
)
from app.services.units import IngredientConversion, to_reference_quantity

#: Seuils des tags nutritionnels dérivés, exprimés en part de l'énergie totale.
#: Calibrables : ils vivent ici, en un seul endroit, pas dans les requêtes.
HIGH_PROTEIN_ENERGY_RATIO = Decimal("0.20")
LOW_CARB_ENERGY_RATIO = Decimal("0.25")

KCAL_PER_G_PROTEIN = Decimal("4")
KCAL_PER_G_CARBS = Decimal("4")


@dataclass(frozen=True)
class IngredientFacts:
    """Vue en lecture seule d'un ingrédient, suffisante pour tous les calculs."""

    slug: str
    reference_unit: ReferenceUnit
    kcal_100: Decimal
    protein_100: Decimal
    carbs_100: Decimal
    fat_100: Decimal
    fiber_100: Decimal = Decimal("0")
    allergens: frozenset[Allergen] = frozenset()
    restriction_flags: frozenset[RestrictionFlag] = frozenset()
    density_g_per_ml: Decimal | None = None
    conversions: tuple[IngredientConversion, ...] = ()
    is_allergen_verified: bool = False


@dataclass(frozen=True)
class DishComponent:
    """Un ingrédient et sa quantité, pour `servings` portions."""

    ingredient: IngredientFacts
    quantity: Decimal
    unit: str


@dataclass(frozen=True)
class NutritionFacts:
    """Valeurs **par portion**."""

    kcal: Decimal = Decimal("0")
    protein_g: Decimal = Decimal("0")
    carbs_g: Decimal = Decimal("0")
    fat_g: Decimal = Decimal("0")
    fiber_g: Decimal = Decimal("0")


@dataclass(frozen=True)
class DerivedDishData:
    """Tout ce qu'un plat tient de ses ingrédients."""

    nutrition: NutritionFacts
    allergens: frozenset[Allergen] = frozenset()
    derived_tags: frozenset[DishTag] = frozenset()
    compatible_restrictions: frozenset[RestrictionType] = frozenset()
    #: Ingrédients dont les allergènes ne sont pas vérifiés : tant que cette
    #: liste n'est pas vide, le plat n'est pas publiable (FN-003).
    unverified_ingredients: tuple[str, ...] = field(default=())


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_nutrition(
    components: list[DishComponent], servings: int
) -> NutritionFacts:
    """D-11 — valeurs nutritionnelles par portion.

        kcal_portion = Σ (quantité_en_unité_de_référence × kcal_100 / 100) / servings

    Les valeurs des ingrédients sont exprimées **pour 100 unités de référence**
    (100 g, 100 ml). Toute quantité exprimée dans une autre unité est convertie
    au préalable ; une conversion manquante lève `UnitConversionError` et
    interrompt le calcul, conformément à FN-012.
    """
    if servings <= 0:
        raise ValueError("servings doit être strictement positif")

    totals = {"kcal": Decimal("0"), "protein": Decimal("0"), "carbs": Decimal("0"),
              "fat": Decimal("0"), "fiber": Decimal("0")}

    for component in components:
        ingredient = component.ingredient
        reference_quantity = to_reference_quantity(
            component.quantity,
            component.unit,
            ingredient.reference_unit,
            density_g_per_ml=ingredient.density_g_per_ml,
            conversions=list(ingredient.conversions),
        )
        ratio = reference_quantity / Decimal("100")
        totals["kcal"] += ratio * ingredient.kcal_100
        totals["protein"] += ratio * ingredient.protein_100
        totals["carbs"] += ratio * ingredient.carbs_100
        totals["fat"] += ratio * ingredient.fat_100
        totals["fiber"] += ratio * ingredient.fiber_100

    divisor = Decimal(servings)
    return NutritionFacts(
        kcal=_round(totals["kcal"] / divisor),
        protein_g=_round(totals["protein"] / divisor),
        carbs_g=_round(totals["carbs"] / divisor),
        fat_g=_round(totals["fat"] / divisor),
        fiber_g=_round(totals["fiber"] / divisor),
    )


def propagate_allergens(components: list[DishComponent]) -> frozenset[Allergen]:
    """FN-008 · 🔴 `dish.allergens = ⋃ ingredient.allergens`.

    Union stricte, y compris les allergènes portés indirectement par un
    ingrédient composé. Un allergène propagé ne peut jamais être retiré : seul
    l'ajout manuel est permis, en aval.
    """
    allergens: set[Allergen] = set()
    for component in components:
        allergens |= set(component.ingredient.allergens)
    return frozenset(allergens)


def unverified_ingredients(components: list[DishComponent]) -> tuple[str, ...]:
    """FN-003 — ingrédients bloquant la publication du plat."""
    return tuple(
        component.ingredient.slug
        for component in components
        if not component.ingredient.is_allergen_verified
    )


def derive_compatible_restrictions(
    components: list[DishComponent],
) -> frozenset[RestrictionType]:
    """FN-004 / FN-009 — restrictions utilisateur que le plat satisfait.

    Sémantique des drapeaux d'ingrédient (`RestrictionFlag`) :

    * `vegetarian` / `vegan` — l'ingrédient *convient* à ce régime ;
    * `pork` / `alcohol` / `lactose` / `gluten` — l'ingrédient *en contient*.

    Un plat est donc végétarien si **tous** ses ingrédients le sont, et sans
    lactose si **aucun** n'en contient. Un plat sans ingrédient ne satisfait
    rien : l'absence de donnée n'est pas une garantie.
    """
    if not components:
        return frozenset()

    flags = [component.ingredient.restriction_flags for component in components]
    satisfied: set[RestrictionType] = set()

    if all(RestrictionFlag.VEGETARIAN in f for f in flags):
        satisfied.add(RestrictionType.VEGETARIAN)
    if all(RestrictionFlag.VEGAN in f for f in flags):
        satisfied.add(RestrictionType.VEGAN)
    if not any(RestrictionFlag.PORK in f for f in flags):
        satisfied.add(RestrictionType.NO_PORK)
    if not any(RestrictionFlag.ALCOHOL in f for f in flags):
        satisfied.add(RestrictionType.NO_ALCOHOL)
    if not any(RestrictionFlag.LACTOSE in f for f in flags):
        satisfied.add(RestrictionType.LACTOSE_FREE)

    gluten_free = not any(RestrictionFlag.GLUTEN in f for f in flags) and not any(
        Allergen.GLUTEN in component.ingredient.allergens for component in components
    )
    if gluten_free:
        satisfied.add(RestrictionType.GLUTEN_FREE)

    return frozenset(satisfied)


def derive_tags(
    components: list[DishComponent],
    nutrition: NutritionFacts,
    compatible: frozenset[RestrictionType],
) -> frozenset[DishTag]:
    """FN-009 — tags **calculés**. Les tags subjectifs (`quick`, `family_meal`,
    `local`, `make_ahead`, `seasonal`) restent saisis à la main."""
    tags: set[DishTag] = set()

    if RestrictionType.VEGETARIAN in compatible:
        tags.add(DishTag.VEGETARIAN)
    if RestrictionType.VEGAN in compatible:
        tags.add(DishTag.VEGAN)
    if RestrictionType.LACTOSE_FREE in compatible:
        tags.add(DishTag.LACTOSE_FREE)
    if RestrictionType.GLUTEN_FREE in compatible:
        tags.add(DishTag.GLUTEN_FREE)

    if nutrition.kcal > 0:
        protein_ratio = nutrition.protein_g * KCAL_PER_G_PROTEIN / nutrition.kcal
        carbs_ratio = nutrition.carbs_g * KCAL_PER_G_CARBS / nutrition.kcal
        if protein_ratio >= HIGH_PROTEIN_ENERGY_RATIO:
            tags.add(DishTag.HIGH_PROTEIN)
        if carbs_ratio <= LOW_CARB_ENERGY_RATIO:
            tags.add(DishTag.LOW_CARB)

    return frozenset(tags)


def derive_dish_data(components: list[DishComponent], servings: int) -> DerivedDishData:
    """Point d'entrée unique : tout ce qu'un plat tient de ses ingrédients."""
    nutrition = compute_nutrition(components, servings)
    compatible = derive_compatible_restrictions(components)
    return DerivedDishData(
        nutrition=nutrition,
        allergens=propagate_allergens(components),
        derived_tags=derive_tags(components, nutrition, compatible),
        compatible_restrictions=compatible,
        unverified_ingredients=unverified_ingredients(components),
    )


__all__ = [
    "DerivedDishData",
    "DishComponent",
    "IngredientFacts",
    "NutritionFacts",
    "compute_nutrition",
    "derive_compatible_restrictions",
    "derive_dish_data",
    "derive_tags",
    "propagate_allergens",
    "unverified_ingredients",
]
