"""Contrat HTTP du catalogue (FN-007 → FN-009).

Deux absences sont significatives et volontaires.

**Aucun schéma d'entrée n'accepte de valeur nutritionnelle de plat, d'allergène
de plat, ni de tag dérivé.** D-11 et FN-008 les rendent calculés ; les accepter,
même pour les ignorer, laisserait croire qu'ils sont saisissables. `extra="forbid"`
transforme la tentative en erreur 422 explicite.

**Aucun schéma d'entrée n'accepte de statut, de validateur ni de date de
publication.** Le cycle de vie passe exclusivement par les points d'entrée
dédiés (`submit`, `validate`, `reject`, `publish`, `archive`), qui journalisent
et vérifient les conditions. Un `PUT` qui pourrait publier contournerait FN-003.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    Allergen,
    CostClass,
    DERIVED_TAGS,
    Difficulty,
    DishStatus,
    DishTag,
    Goal,
    IngredientCategory,
    IngredientStatus,
    MealType,
    ReferenceUnit,
    RestrictionFlag,
)

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------
# FN-007 — ingrédients
# --------------------------------------------------------------------------


class AliasIn(ApiModel):
    alias: str = Field(min_length=1, max_length=200)
    language: str = Field(default="fr", pattern=r"^[a-z]{2}$")


class ConversionIn(ApiModel):
    """FN-012 — facteur **mesuré**, propre à l'ingrédient. `source` documente
    la mesure ; une conversion locale ne s'estime pas."""

    from_unit: str = Field(min_length=1, max_length=40)
    to_unit: str = Field(min_length=1, max_length=40)
    factor: Decimal = Field(gt=0)
    source: str = Field(min_length=3, max_length=120)
    measured_at: date | None = None

    @model_validator(mode="after")
    def _units_differ(self) -> "ConversionIn":
        if self.from_unit.lower() == self.to_unit.lower():
            raise ValueError("from_unit et to_unit doivent différer")
        return self


class IngredientIn(ApiModel):
    slug: str = Field(pattern=SLUG_PATTERN, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    category: IngredientCategory
    reference_unit: ReferenceUnit

    kcal_100: Decimal = Field(ge=0)
    protein_100: Decimal = Field(ge=0)
    carbs_100: Decimal = Field(ge=0)
    fat_100: Decimal = Field(ge=0)
    fiber_100: Decimal = Field(default=Decimal("0"), ge=0)
    #: Obligatoire : aucune valeur nutritionnelle ne doit être inventée (FN-007).
    nutrition_source: str = Field(min_length=3, max_length=255)

    restriction_flags: list[RestrictionFlag] = Field(default_factory=list)
    locally_available: bool = True
    seasonality: list[int] = Field(default_factory=list, max_length=12)
    density_g_per_ml: Decimal | None = Field(default=None, gt=0)
    image_url: str | None = Field(default=None, max_length=500)
    status: IngredientStatus = IngredientStatus.ACTIVE

    aliases: list[AliasIn] = Field(default_factory=list, max_length=20)
    unit_conversions: list[ConversionIn] = Field(default_factory=list, max_length=20)

    @field_validator("seasonality")
    @classmethod
    def _months(cls, value: list[int]) -> list[int]:
        if any(m < 1 or m > 12 for m in value):
            raise ValueError("les mois de saison vont de 1 à 12")
        return sorted(set(value))


class VerifyAllergensIn(ApiModel):
    """FN-003 · 🔴 La signature de la grille allergènes.

    `allergens: []` signifie « aucun allergène » — c'est une **affirmation**,
    pas une absence de donnée, et c'est pourquoi elle exige une source. Le
    validateur n'est pas dans le corps : il vient du jeton, pour que la
    traçabilité de responsabilité ne repose pas sur du déclaratif.
    """

    allergens: list[Allergen] = Field(default_factory=list)
    source: str = Field(min_length=3, max_length=255)

    @field_validator("allergens")
    @classmethod
    def _dedupe(cls, value: list[Allergen]) -> list[Allergen]:
        return list(dict.fromkeys(value))


class ConversionOut(OutModel):
    from_unit: str
    to_unit: str
    factor: Decimal
    source: str
    measured_at: datetime | None


class AliasOut(OutModel):
    alias: str
    language: str


class IngredientOut(OutModel):
    id: UUID
    slug: str
    name: str
    category: IngredientCategory
    reference_unit: ReferenceUnit
    kcal_100: Decimal
    protein_100: Decimal
    carbs_100: Decimal
    fat_100: Decimal
    fiber_100: Decimal
    nutrition_source: str
    allergens: list[str]
    allergen_source: str | None
    allergen_verified_by: str | None
    allergen_verified_at: datetime | None
    #: FN-003 — « vérifié » signifie *signé*, pas « sans allergène ».
    allergen_verified: bool
    restriction_flags: list[str]
    locally_available: bool
    seasonality: list[int]
    density_g_per_ml: Decimal | None
    image_url: str | None
    status: IngredientStatus
    aliases: list[AliasOut] = Field(default_factory=list)
    unit_conversions: list[ConversionOut] = Field(default_factory=list)


# --------------------------------------------------------------------------
# FN-008 / FN-009 — plats
# --------------------------------------------------------------------------


class DishIngredientIn(ApiModel):
    ingredient: str = Field(pattern=SLUG_PATTERN, max_length=200)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=255)


class DishIn(ApiModel):
    slug: str = Field(pattern=SLUG_PATTERN, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    image_url: str | None = Field(default=None, max_length=500)

    meal_types: list[MealType] = Field(min_length=1)
    origin: str | None = Field(default=None, max_length=120)
    prep_time_min: int = Field(default=0, ge=0)
    cook_time_min: int = Field(default=0, ge=0)
    difficulty: Difficulty = Difficulty.EASY
    servings: int = Field(default=1, gt=0)

    estimated_cost: Decimal | None = Field(default=None, ge=0)
    cost_class: CostClass = CostClass.MEDIUM
    currency: str = Field(default="MGA", min_length=3, max_length=3)

    compatible_goals: list[Goal] = Field(default_factory=list)
    #: Tags **subjectifs** uniquement. Les six tags dérivables sont calculés
    #: depuis les ingrédients et refusés ici (FN-009).
    tags: list[DishTag] = Field(default_factory=list)

    ingredients: list[DishIngredientIn] = Field(min_length=1, max_length=60)
    steps: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("tags")
    @classmethod
    def _no_derived_tags(cls, value: list[DishTag]) -> list[DishTag]:
        derived = sorted(t.value for t in value if t in DERIVED_TAGS)
        if derived:
            raise ValueError(
                "ces tags sont calculés depuis les ingrédients et ne se "
                "saisissent pas : " + ", ".join(derived)
            )
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _unique_ingredients(self) -> "DishIn":
        slugs = [entry.ingredient for entry in self.ingredients]
        doublons = sorted({s for s in slugs if slugs.count(s) > 1})
        if doublons:
            raise ValueError(
                "un ingrédient ne peut figurer qu'une fois : " + ", ".join(doublons)
            )
        return self


class RejectIn(ApiModel):
    """FN-008 — un refus sans motif n'aide personne à corriger le plat."""

    reason: str = Field(min_length=5, max_length=2000)


class DishIngredientOut(OutModel):
    ingredient: str
    name: str
    quantity: Decimal
    unit: str
    note: str | None
    allergen_verified: bool


class NutritionOut(BaseModel):
    kcal: Decimal | None = None
    protein_g: Decimal | None = None
    carbs_g: Decimal | None = None
    fat_g: Decimal | None = None
    fiber_g: Decimal | None = None


class DishOut(OutModel):
    id: UUID
    slug: str
    name: str
    description: str | None
    image_url: str | None
    meal_types: list[str]
    origin: str | None
    prep_time_min: int
    cook_time_min: int
    difficulty: Difficulty
    servings: int

    nutrition: NutritionOut
    nutrition_computed_at: datetime | None

    estimated_cost: Decimal | None
    cost_class: CostClass
    currency: str

    allergens: list[str]
    compatible_goals: list[str]
    compatible_restrictions: list[str]
    tags_derived: list[str]
    tags_manual: list[str]

    status: DishStatus
    author_id: str | None
    validated_by: str | None
    validated_at: datetime | None
    published_at: datetime | None
    rejection_reason: str | None

    ingredients: list[DishIngredientOut] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)

    #: FN-008 — vide quand le plat peut être publié. Exposé sur chaque plat pour
    #: que la file de validation n'ait pas à le recalculer côté client.
    publication_blockers: list[str] = Field(default_factory=list)


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class IngredientPage(Page):
    items: list[IngredientOut]


class DishPage(Page):
    items: list[DishOut]


__all__ = [
    "AliasIn",
    "AliasOut",
    "ConversionIn",
    "ConversionOut",
    "DishIn",
    "DishIngredientIn",
    "DishIngredientOut",
    "DishOut",
    "DishPage",
    "IngredientIn",
    "IngredientOut",
    "IngredientPage",
    "NutritionOut",
    "RejectIn",
    "VerifyAllergensIn",
]
