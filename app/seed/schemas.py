"""Contrat des fichiers de seed (FN-040, D-12).

Ces modèles sont la **définition formelle** du format YAML attendu. Ils sont
volontairement stricts : `extra="forbid"` transforme une faute de frappe dans un
nom de champ en erreur immédiate, plutôt qu'en donnée silencieusement ignorée.

Une donnée nutritionnelle silencieusement ignorée, c'est un plat faux dans un
programme réel.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import (
    Allergen,
    CostClass,
    DERIVED_TAGS,
    Difficulty,
    DishTag,
    Goal,
    IngredientCategory,
    IngredientStatus,
    MealType,
    ReferenceUnit,
    RestrictionFlag,
)


class SeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NutritionBlock(SeedModel):
    """Valeurs **pour 100 unités de référence** (100 g, 100 ml).

    `source` est obligatoire : aucune valeur ne doit être inventée. Par ordre de
    pertinence pour le contexte malgache — FAO/INFOODS Afrique de l'Ouest,
    CIQUAL (ANSES), USDA FoodData Central.
    """

    source: str = Field(min_length=3)
    kcal_100: Decimal = Field(ge=0)
    protein_100: Decimal = Field(ge=0)
    carbs_100: Decimal = Field(ge=0)
    fat_100: Decimal = Field(ge=0)
    fiber_100: Decimal = Field(default=Decimal("0"), ge=0)


class AllergenBlock(SeedModel):
    """FN-003 · 🔴 Déclaration des allergènes et de sa vérification.

    `values: []` signifie « aucun allergène » — c'est une **affirmation**, pas
    une absence de donnée. Elle n'a de valeur que signée : sans `source`,
    `verified_by` et `verified_at`, l'ingrédient reste non vérifié et rend tout
    plat qui l'utilise non publiable.
    """

    values: list[Allergen] = Field(default_factory=list)
    source: str | None = None
    verified_by: str | None = None
    verified_at: date | None = None

    @property
    def is_verified(self) -> bool:
        return (
            self.source is not None
            and self.verified_by is not None
            and self.verified_at is not None
        )

    @model_validator(mode="after")
    def _verification_is_atomic(self) -> "AllergenBlock":
        provided = [self.source, self.verified_by, self.verified_at]
        if any(v is not None for v in provided) and not all(
            v is not None for v in provided
        ):
            raise ValueError(
                "la vérification allergène est atomique : renseigner source, "
                "verified_by et verified_at, ou aucun des trois"
            )
        return self


class AliasEntry(SeedModel):
    alias: str = Field(min_length=1)
    language: str = Field(default="fr", pattern=r"^[a-z]{2}$")


class ConversionEntry(SeedModel):
    """FN-012 — facteur **mesuré**, propre à l'ingrédient.

    `source` documente la mesure : `measured` (pesée réelle), `reference_table`,
    `manufacturer`. Ne jamais estimer un facteur local.
    """

    from_unit: str = Field(min_length=1)
    to_unit: str = Field(min_length=1)
    factor: Decimal = Field(gt=0)
    source: str = Field(min_length=3)
    measured_at: date | None = None

    @model_validator(mode="after")
    def _units_differ(self) -> "ConversionEntry":
        if self.from_unit.lower() == self.to_unit.lower():
            raise ValueError("from_unit et to_unit doivent différer")
        return self


class SeedIngredient(SeedModel):
    """FN-007 — un ingrédient du catalogue."""

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
    name: str = Field(min_length=1, max_length=200)
    category: IngredientCategory
    reference_unit: ReferenceUnit
    nutrition: NutritionBlock
    allergens: AllergenBlock = Field(default_factory=AllergenBlock)
    restriction_flags: list[RestrictionFlag] = Field(default_factory=list)
    locally_available: bool = True
    seasonality: list[int] = Field(default_factory=list)
    density_g_per_ml: Decimal | None = Field(default=None, gt=0)
    image_url: str | None = None
    status: IngredientStatus = IngredientStatus.ACTIVE
    aliases: list[AliasEntry] = Field(default_factory=list)
    unit_conversions: list[ConversionEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_seasonality(self) -> "SeedIngredient":
        invalid = [m for m in self.seasonality if not 1 <= m <= 12]
        if invalid:
            raise ValueError(f"mois de saison invalides : {invalid}")
        return self


class DishIngredientEntry(SeedModel):
    """Référence à un ingrédient par son `slug`, pour `servings` portions."""

    ingredient: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=255)


class ValidationBlock(SeedModel):
    """FN-008 — signature du nutritionniste.

    `validated_by` est la traçabilité de responsabilité en cas d'incident : il
    est copié dans `dishes.validated_by`. Il doit différer de l'auteur.
    """

    validated_by: str | None = None
    validated_at: date | None = None
    #: Demande de publication. Refusée si un ingrédient n'est pas vérifié.
    publish: bool = False

    @model_validator(mode="after")
    def _validation_is_atomic(self) -> "ValidationBlock":
        if (self.validated_by is None) != (self.validated_at is None):
            raise ValueError(
                "renseigner validated_by et validated_at ensemble, ou aucun des deux"
            )
        if self.publish and self.validated_by is None:
            raise ValueError("publish: true exige une validation signée")
        return self


class SeedDish(SeedModel):
    """FN-008 — un plat du catalogue.

    Les valeurs nutritionnelles ne figurent **pas** dans ce contrat : elles sont
    calculées depuis les ingrédients (D-11). Les allergènes non plus : ils sont
    propagés. Les tags dérivables non plus : ils sont déduits.
    """

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    image_url: str | None = None

    meal_types: list[MealType] = Field(min_length=1)
    origin: str | None = Field(default=None, max_length=120)
    prep_time_min: int = Field(default=0, ge=0)
    cook_time_min: int = Field(default=0, ge=0)
    difficulty: Difficulty = Difficulty.EASY
    servings: int = Field(default=1, gt=0)

    estimated_cost: Decimal | None = Field(default=None, ge=0)
    cost_class: CostClass = CostClass.MEDIUM
    currency: str = Field(default="MGA", pattern=r"^[A-Z]{3}$")

    ingredients: list[DishIngredientEntry] = Field(min_length=1)
    steps: list[str] = Field(default_factory=list)
    #: Tags **subjectifs** uniquement (`quick`, `local`, `family_meal`,
    #: `make_ahead`, `seasonal`, `economical`, `muscle_gain`, `weight_loss`).
    tags: list[DishTag] = Field(default_factory=list)
    compatible_goals: list[Goal] = Field(default_factory=list)

    author: str | None = None
    validation: ValidationBlock = Field(default_factory=ValidationBlock)

    @model_validator(mode="after")
    def _no_derived_tags(self) -> "SeedDish":
        """FN-009 — les tags dérivables sont calculés, jamais saisis."""
        forbidden = sorted(tag.value for tag in self.tags if tag in DERIVED_TAGS)
        if forbidden:
            raise ValueError(
                f"tags dérivés interdits en saisie : {forbidden} — ils sont "
                "calculés depuis les ingrédients"
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_ingredient(self) -> "SeedDish":
        slugs = [entry.ingredient for entry in self.ingredients]
        duplicates = sorted({s for s in slugs if slugs.count(s) > 1})
        if duplicates:
            raise ValueError(f"ingrédients en double : {duplicates}")
        return self

    @model_validator(mode="after")
    def _validator_differs_from_author(self) -> "SeedDish":
        if (
            self.validation.validated_by is not None
            and self.author is not None
            and self.validation.validated_by == self.author
        ):
            raise ValueError("le validateur ne peut pas être l'auteur du plat")
        return self


__all__ = [
    "AliasEntry",
    "AllergenBlock",
    "ConversionEntry",
    "DishIngredientEntry",
    "NutritionBlock",
    "SeedDish",
    "SeedIngredient",
    "ValidationBlock",
]
