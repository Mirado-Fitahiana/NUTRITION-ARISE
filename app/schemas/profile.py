"""Contrat HTTP du profil nutritionnel (FN-001 → FN-004).

Les bornes reprennent **exactement** les contraintes `CHECK` du schéma. Cette
duplication est voulue : la base est le dernier rempart, mais un refus à
l'entrée produit un message compréhensible plutôt qu'une violation de
contrainte, et FN-001 exige un message explicite.

Un point de sécurité traverse tout le module : `external_user_id` n'apparaît
**dans aucun schéma d'entrée**. Il vient du claim `sub` du jeton, jamais du
corps de la requête — sinon n'importe quel appelant lirait le profil d'autrui.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    ActivityLevel,
    Allergen,
    Goal,
    RestrictionType,
    Sex,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------
# FN-001 — profil
# --------------------------------------------------------------------------


class ProfileIn(ApiModel):
    goal: Goal
    weight_kg: Decimal = Field(ge=25, le=300)
    height_cm: Decimal = Field(ge=100, le=250)
    birth_date: date
    sex: Sex
    activity_level: ActivityLevel
    meals_per_day: int = Field(default=3, ge=3, le=5)
    household_size: int = Field(default=1, ge=1)

    daily_budget: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="MGA", min_length=3, max_length=3)
    city: str | None = Field(default=None, max_length=120)
    district: str | None = Field(default=None, max_length=120)
    travel_radius_km: Decimal | None = Field(default=None, ge=0)

    # FN-039 — champs déclaratifs, jamais présélectionnés côté client.
    declared_pregnancy: bool = False
    declared_breastfeeding: bool = False
    declared_medical_condition: bool = False
    #: Accusé de lecture de l'avertissement. Une fois vrai, il ne redevient
    #: jamais faux : la date d'acceptation est conservée.
    disclaimer_accepted: bool = False

    @field_validator("birth_date")
    @classmethod
    def _plausible_birth_date(cls, value: date) -> date:
        today = date.today()
        if value >= today:
            raise ValueError("la date de naissance doit être dans le passé")
        if value.year < today.year - 120:
            raise ValueError("la date de naissance n'est pas plausible")
        return value

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, value: str) -> str:
        return value.upper()


class ProfileOut(OutModel):
    goal: Goal
    weight_kg: Decimal
    height_cm: Decimal
    birth_date: date
    sex: Sex
    activity_level: ActivityLevel
    meals_per_day: int
    household_size: int
    daily_budget: Decimal | None
    currency: str
    city: str | None
    district: str | None
    travel_radius_km: Decimal | None
    declared_pregnancy: bool
    declared_breastfeeding: bool
    declared_medical_condition: bool
    disclaimer_accepted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    #: Dérivés, calculés à la lecture — jamais stockés, donc jamais périmés.
    age: int | None = None
    bmi: Decimal | None = None


class ProfileHistoryOut(OutModel):
    changed_at: datetime
    changed_fields: list[str]
    correlation_id: str | None


# --------------------------------------------------------------------------
# FN-002 — préférences
# --------------------------------------------------------------------------


class PreferencesIn(ApiModel):
    """Modulateurs de score. Les aliments refusés sont la seule partie
    bloquante de ce bloc, et ils sont traités comme une exclusion dure."""

    prefer_local_products: bool = False
    prefer_quick_dishes: bool = False
    prefer_easy_dishes: bool = False
    prefer_economical_dishes: bool = False
    vegetarian_preference: bool = False

    preferred_cuisines: list[str] = Field(default_factory=list, max_length=20)
    preferred_protein_types: list[str] = Field(default_factory=list, max_length=20)
    max_food_frequency: int | None = Field(default=None, gt=0)

    #: Slugs d'ingrédients. Le slug, jamais l'identifiant technique : c'est la
    #: clé fonctionnelle du catalogue, stable entre environnements (D-12).
    favorite_ingredients: list[str] = Field(default_factory=list, max_length=100)
    disliked_ingredients: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _no_ingredient_both_ways(self) -> "PreferencesIn":
        both = set(self.favorite_ingredients) & set(self.disliked_ingredients)
        if both:
            raise ValueError(
                "un ingrédient ne peut pas être à la fois favori et refusé : "
                + ", ".join(sorted(both))
            )
        return self


class PreferencesOut(OutModel):
    prefer_local_products: bool
    prefer_quick_dishes: bool
    prefer_easy_dishes: bool
    prefer_economical_dishes: bool
    vegetarian_preference: bool
    preferred_cuisines: list[str]
    preferred_protein_types: list[str]
    max_food_frequency: int | None
    favorite_ingredients: list[str] = Field(default_factory=list)
    disliked_ingredients: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# FN-003 — allergies
# --------------------------------------------------------------------------


class AllergiesIn(ApiModel):
    """Remplacement intégral de la liste. Un `PUT` partiel serait ambigu, et
    une ambiguïté sur des allergies n'est pas acceptable."""

    allergens: list[Allergen] = Field(default_factory=list)

    @field_validator("allergens")
    @classmethod
    def _dedupe(cls, value: list[Allergen]) -> list[Allergen]:
        return list(dict.fromkeys(value))


class AllergyOut(OutModel):
    allergen: Allergen
    declared_at: datetime


# --------------------------------------------------------------------------
# FN-004 — restrictions
# --------------------------------------------------------------------------


class RestrictionIn(ApiModel):
    restriction_type: RestrictionType
    custom_label: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _label_required_for_free_form(self) -> "RestrictionIn":
        needs_label = {
            RestrictionType.CUSTOM,
            RestrictionType.RELIGIOUS,
            RestrictionType.FORBIDDEN_FOOD,
        }
        if self.restriction_type in needs_label and not self.custom_label:
            raise ValueError(
                f"la restriction « {self.restriction_type.value} » exige un libellé"
            )
        return self


class RestrictionsIn(ApiModel):
    restrictions: list[RestrictionIn] = Field(default_factory=list, max_length=30)


class RestrictionOut(OutModel):
    restriction_type: RestrictionType
    custom_label: str | None
    is_normalized: bool
    needs_admin_review: bool
    normalized_tags: list[str]
    normalized_ingredients: list[str] = Field(default_factory=list)
    created_at: datetime


class RestrictionsOut(BaseModel):
    restrictions: list[RestrictionOut]
    #: FN-004 — restrictions enregistrées mais **non appliquées**, en attente de
    #: normalisation. Le client doit les montrer : les taire reviendrait à
    #: laisser croire qu'elles sont prises en compte.
    pending_review: list[str] = Field(default_factory=list)


__all__ = [
    "AllergiesIn",
    "AllergyOut",
    "PreferencesIn",
    "PreferencesOut",
    "ProfileHistoryOut",
    "ProfileIn",
    "ProfileOut",
    "RestrictionIn",
    "RestrictionOut",
    "RestrictionsIn",
    "RestrictionsOut",
]
