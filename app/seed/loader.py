"""Chargement idempotent du catalogue (FN-040, D-12).

Le seed est **rejouable à l'identique** en développement, en recette et en
production : la clé fonctionnelle est le `slug`, jamais un identifiant technique.
Rejouer deux fois produit exactement le même état.

Le chargeur est aussi le **garde-fou de publication** : il refuse de publier un
plat dont un ingrédient n'a pas ses allergènes vérifiés (FN-003). Ce refus est
attendu et massif au démarrage du lot 1 — c'est le signal que la validation
nutritionniste n'est pas terminée, pas un défaut à contourner.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import (
    Dish,
    DishAllergen,
    DishIngredient,
    DishStep,
    DishTagLink,
    Ingredient,
    IngredientAlias,
    IngredientUnitConversion,
)
from app.models.enums import DishStatus
from app.seed.schemas import SeedDish, SeedIngredient
from app.services.nutrition import (
    DishComponent,
    IngredientFacts,
    derive_dish_data,
)
from app.services.units import IngredientConversion, UnitConversionError


@dataclass
class SeedReport:
    """Compte rendu d'exécution. `errors` non vide ⇒ code de sortie non nul."""

    ingredients_created: int = 0
    ingredients_updated: int = 0
    dishes_created: int = 0
    dishes_updated: int = 0
    dishes_published: int = 0
    #: Plats dont la publication a été refusée, avec le motif.
    publication_blocked: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [
            "",
            "Ingrédients  : "
            f"{self.ingredients_created} créés, {self.ingredients_updated} mis à jour",
            "Plats        : "
            f"{self.dishes_created} créés, {self.dishes_updated} mis à jour, "
            f"{self.dishes_published} publiés",
        ]
        if self.publication_blocked:
            lines.append("")
            lines.append(
                f"Publication refusée pour {len(self.publication_blocked)} plat(s) :"
            )
            for slug, reason in self.publication_blocked:
                lines.append(f"  · {slug} — {reason}")
        if self.errors:
            lines.append("")
            lines.append(f"{len(self.errors)} erreur(s) :")
            lines.extend(f"  ✗ {message}" for message in self.errors)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Lecture et validation des fichiers
# --------------------------------------------------------------------------


def _load_yaml_documents(directory: Path) -> list[tuple[Path, int, dict[str, Any]]]:
    """Retourne `(fichier, index, données)` pour chaque entrée trouvée.

    Le fichier et l'index sont conservés afin qu'une erreur de validation
    désigne précisément l'entrée fautive — sans quoi corriger un fichier de
    150 ingrédients devient un jeu de piste.
    """
    documents: list[tuple[Path, int, dict[str, Any]]] = []
    if not directory.exists():
        return documents

    for path in sorted(directory.rglob("*.yaml")) + sorted(directory.rglob("*.yml")):
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(content, list):
            raise ValueError(
                f"{path} : le fichier doit contenir une liste d'entrées YAML"
            )
        for index, entry in enumerate(content):
            documents.append((path, index, entry))
    return documents


def _describe(path: Path, index: int, exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<racine>'} : {err['msg']}"
        for err in exc.errors()
    )
    return f"{path.name}[{index}] — {problems}"


def parse_ingredients(directory: Path) -> tuple[dict[str, SeedIngredient], list[str]]:
    parsed: dict[str, SeedIngredient] = {}
    errors: list[str] = []

    for path, index, entry in _load_yaml_documents(directory):
        try:
            ingredient = SeedIngredient.model_validate(entry)
        except ValidationError as exc:
            errors.append(_describe(path, index, exc))
            continue
        if ingredient.slug in parsed:
            errors.append(f"{path.name}[{index}] — slug déjà défini : {ingredient.slug}")
            continue
        parsed[ingredient.slug] = ingredient

    return parsed, errors


def parse_dishes(directory: Path) -> tuple[dict[str, SeedDish], list[str]]:
    parsed: dict[str, SeedDish] = {}
    errors: list[str] = []

    for path, index, entry in _load_yaml_documents(directory):
        try:
            dish = SeedDish.model_validate(entry)
        except ValidationError as exc:
            errors.append(_describe(path, index, exc))
            continue
        if dish.slug in parsed:
            errors.append(f"{path.name}[{index}] — slug déjà défini : {dish.slug}")
            continue
        parsed[dish.slug] = dish

    return parsed, errors


# --------------------------------------------------------------------------
# Conversion seed → structures de calcul
# --------------------------------------------------------------------------


def build_facts(seed: SeedIngredient) -> IngredientFacts:
    return IngredientFacts(
        slug=seed.slug,
        reference_unit=seed.reference_unit,
        kcal_100=seed.nutrition.kcal_100,
        protein_100=seed.nutrition.protein_100,
        carbs_100=seed.nutrition.carbs_100,
        fat_100=seed.nutrition.fat_100,
        fiber_100=seed.nutrition.fiber_100,
        allergens=frozenset(seed.allergens.values),
        restriction_flags=frozenset(seed.restriction_flags),
        density_g_per_ml=seed.density_g_per_ml,
        conversions=tuple(
            IngredientConversion(c.from_unit, c.to_unit, c.factor)
            for c in seed.unit_conversions
        ),
        is_allergen_verified=seed.allergens.is_verified,
    )


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------


def _replace_collection(session: Session, collection: list, new_items: list) -> None:
    """Vide puis repeuple une collection fille.

    Le `flush` intermédiaire n'est pas cosmétique : sans lui, SQLAlchemy émet
    les `INSERT` des nouvelles lignes **avant** les `DELETE` des anciennes, et le
    second passage du seed viole les contraintes d'unicité. C'est exactement le
    genre de défaut qu'un seed non rejoué ne révèle jamais.
    """
    if collection:
        collection.clear()
        session.flush()
    collection.extend(new_items)


def _upsert_ingredient(
    session: Session, seed: SeedIngredient, report: SeedReport
) -> Ingredient:
    ingredient = session.scalar(
        select(Ingredient)
        .where(Ingredient.slug == seed.slug)
        .options(
            selectinload(Ingredient.aliases),
            selectinload(Ingredient.unit_conversions),
        )
    )
    if ingredient is None:
        ingredient = Ingredient(id=uuid.uuid4(), slug=seed.slug)
        session.add(ingredient)
        report.ingredients_created += 1
    else:
        report.ingredients_updated += 1

    ingredient.name = seed.name
    ingredient.category = seed.category
    ingredient.reference_unit = seed.reference_unit
    ingredient.kcal_100 = seed.nutrition.kcal_100
    ingredient.protein_100 = seed.nutrition.protein_100
    ingredient.carbs_100 = seed.nutrition.carbs_100
    ingredient.fat_100 = seed.nutrition.fat_100
    ingredient.fiber_100 = seed.nutrition.fiber_100
    ingredient.nutrition_source = seed.nutrition.source

    ingredient.allergens = [allergen.value for allergen in seed.allergens.values]
    ingredient.allergen_source = seed.allergens.source
    ingredient.allergen_verified_by = seed.allergens.verified_by
    ingredient.allergen_verified_at = (
        datetime.combine(seed.allergens.verified_at, datetime.min.time(), tzinfo=UTC)
        if seed.allergens.verified_at
        else None
    )

    ingredient.restriction_flags = [flag.value for flag in seed.restriction_flags]
    ingredient.locally_available = seed.locally_available
    ingredient.seasonality = list(seed.seasonality)
    ingredient.density_g_per_ml = seed.density_g_per_ml
    ingredient.image_url = seed.image_url
    ingredient.status = seed.status

    # Les collections filles sont remplacées : c'est ce qui rend le rejeu
    # idempotent même après suppression d'un alias dans le YAML.
    _replace_collection(
        session,
        ingredient.aliases,
        [
            IngredientAlias(id=uuid.uuid4(), alias=a.alias, language=a.language)
            for a in seed.aliases
        ],
    )
    _replace_collection(
        session,
        ingredient.unit_conversions,
        [
            IngredientUnitConversion(
                id=uuid.uuid4(),
                from_unit=c.from_unit,
                to_unit=c.to_unit,
                factor=c.factor,
                source=c.source,
                measured_at=(
                    datetime.combine(c.measured_at, datetime.min.time(), tzinfo=UTC)
                    if c.measured_at
                    else None
                ),
            )
            for c in seed.unit_conversions
        ],
    )
    return ingredient


def _resolve_status(
    seed: SeedDish, unverified: tuple[str, ...], report: SeedReport
) -> DishStatus:
    """FN-003 / FN-008 — décide du statut, et refuse la publication si besoin."""
    if not seed.validation.publish:
        return (
            DishStatus.VALIDATED
            if seed.validation.validated_by
            else DishStatus.DRAFT
        )

    if unverified:
        report.publication_blocked.append(
            (
                seed.slug,
                "allergènes non vérifiés pour : " + ", ".join(sorted(unverified)),
            )
        )
        return DishStatus.PENDING_VALIDATION

    report.dishes_published += 1
    return DishStatus.PUBLISHED


def _upsert_dish(
    session: Session,
    seed: SeedDish,
    facts: dict[str, IngredientFacts],
    ingredient_ids: dict[str, uuid.UUID],
    report: SeedReport,
) -> None:
    components = [
        DishComponent(
            ingredient=facts[entry.ingredient],
            quantity=entry.quantity,
            unit=entry.unit,
        )
        for entry in seed.ingredients
    ]

    try:
        derived = derive_dish_data(components, seed.servings)
    except UnitConversionError as exc:
        report.errors.append(f"{seed.slug} — {exc}")
        return

    status = _resolve_status(seed, derived.unverified_ingredients, report)

    dish = session.scalar(
        select(Dish)
        .where(Dish.slug == seed.slug)
        .options(
            selectinload(Dish.ingredients),
            selectinload(Dish.steps),
            selectinload(Dish.tags),
            selectinload(Dish.allergens),
        )
    )
    if dish is None:
        dish = Dish(id=uuid.uuid4(), slug=seed.slug)
        session.add(dish)
        report.dishes_created += 1
    else:
        report.dishes_updated += 1

    dish.name = seed.name
    dish.description = seed.description
    dish.image_url = seed.image_url
    dish.meal_types = [meal_type.value for meal_type in seed.meal_types]
    dish.origin = seed.origin
    dish.prep_time_min = seed.prep_time_min
    dish.cook_time_min = seed.cook_time_min
    dish.difficulty = seed.difficulty
    dish.servings = seed.servings

    # D-11 — jamais saisies, toujours recalculées.
    dish.kcal_portion = derived.nutrition.kcal
    dish.protein_portion = derived.nutrition.protein_g
    dish.carbs_portion = derived.nutrition.carbs_g
    dish.fat_portion = derived.nutrition.fat_g
    dish.fiber_portion = derived.nutrition.fiber_g
    dish.nutrition_computed_at = datetime.now(UTC)

    dish.estimated_cost = seed.estimated_cost
    dish.cost_class = seed.cost_class
    dish.currency = seed.currency
    dish.compatible_goals = [goal.value for goal in seed.compatible_goals]
    dish.compatible_restrictions = sorted(
        restriction.value for restriction in derived.compatible_restrictions
    )

    dish.status = status
    dish.author_id = seed.author
    dish.validated_by = seed.validation.validated_by
    dish.validated_at = (
        datetime.combine(seed.validation.validated_at, datetime.min.time(), tzinfo=UTC)
        if seed.validation.validated_at
        else None
    )
    dish.published_at = datetime.now(UTC) if status is DishStatus.PUBLISHED else None

    _replace_collection(
        session,
        dish.ingredients,
        [
            DishIngredient(
                id=uuid.uuid4(),
                ingredient_id=ingredient_ids[entry.ingredient],
                quantity=entry.quantity,
                unit=entry.unit,
                display_order=order,
                note=entry.note,
            )
            for order, entry in enumerate(seed.ingredients)
        ],
    )
    _replace_collection(
        session,
        dish.steps,
        [
            DishStep(id=uuid.uuid4(), step_number=number, instruction=text)
            for number, text in enumerate(seed.steps, start=1)
        ],
    )
    # Tags saisis (subjectifs) + tags dérivés, marqués comme tels.
    _replace_collection(
        session,
        dish.tags,
        [DishTagLink(tag=tag, is_derived=False) for tag in seed.tags]
        + [
            DishTagLink(tag=tag, is_derived=True)
            for tag in sorted(derived.derived_tags, key=lambda t: t.value)
            if tag not in seed.tags
        ],
    )
    _replace_collection(
        session,
        dish.allergens,
        [
            DishAllergen(allergen=allergen, is_manual=False)
            for allergen in sorted(derived.allergens, key=lambda a: a.value)
        ],
    )


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------


def run_seed(
    session: Session | None, seeds_dir: Path, *, dry_run: bool = False
) -> SeedReport:
    """Applique le seed. En `dry_run`, tout est validé et calculé, rien n'est écrit
    et `session` peut être `None`."""
    report = SeedReport()

    ingredients, ingredient_errors = parse_ingredients(seeds_dir / "ingredients")
    dishes, dish_errors = parse_dishes(seeds_dir / "dishes")
    report.errors.extend(ingredient_errors)
    report.errors.extend(dish_errors)

    # Références croisées : un plat ne peut pas citer un ingrédient inconnu.
    for dish in dishes.values():
        unknown = [
            entry.ingredient
            for entry in dish.ingredients
            if entry.ingredient not in ingredients
        ]
        if unknown:
            report.errors.append(
                f"{dish.slug} — ingrédient(s) inconnu(s) : {', '.join(sorted(unknown))}"
            )

    if report.errors:
        return report

    facts = {slug: build_facts(seed) for slug, seed in ingredients.items()}

    if dry_run:
        # Valide les calculs — c'est là que les conversions manquantes sortent.
        for dish in dishes.values():
            components = [
                DishComponent(facts[e.ingredient], e.quantity, e.unit)
                for e in dish.ingredients
            ]
            try:
                derived = derive_dish_data(components, dish.servings)
            except UnitConversionError as exc:
                report.errors.append(f"{dish.slug} — {exc}")
                continue
            _resolve_status(dish, derived.unverified_ingredients, report)
        report.ingredients_created = len(ingredients)
        report.dishes_created = len(dishes)
        return report

    ingredient_ids: dict[str, uuid.UUID] = {}
    for seed in ingredients.values():
        entity = _upsert_ingredient(session, seed, report)
        session.flush()
        ingredient_ids[seed.slug] = entity.id

    for dish in dishes.values():
        _upsert_dish(session, dish, facts, ingredient_ids, report)
        session.flush()

    if report.errors:
        session.rollback()
    else:
        session.commit()

    return report


__all__ = ["SeedReport", "build_facts", "parse_dishes", "parse_ingredients", "run_seed"]
