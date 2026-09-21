"""Contrats d'entrée de la plateforme de pilotage (plan §5 à §8).

`extra="forbid"` partout, comme le reste du service : un champ inattendu est une
erreur de client, pas une option silencieusement ignorée.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ActivityLevel, Allergen, Goal, RestrictionType, Sex


class PilotageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Collecte
# --------------------------------------------------------------------------


class RunIn(PilotageModel):
    source: str = Field(min_length=1, max_length=64)
    mode: Literal["structure", "collecte"]
    #: Sous-ensemble des pages de la source. Absent : toutes (dans la limite du plafond).
    pages: list[str] | None = Field(default=None, max_length=50)


class SourcePatch(PilotageModel):
    is_active: bool | None = None
    #: Le plancher de 3 s est aussi porté par la base et par le moteur.
    delay_seconds: Decimal | None = Field(default=None, ge=3, le=120)
    max_pages: int | None = Field(default=None, ge=1, le=50)
    pages: list[str] | None = Field(default=None, max_length=50)
    #: Vrai : attester les CGU maintenant, au nom de l'appelant. Faux : retirer l'attestation.
    tos_attested: bool | None = None
    tos_url: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class AliasIn(PilotageModel):
    label: str = Field(min_length=1, max_length=200)
    ingredient: str = Field(min_length=1, max_length=200)
    language: Literal["fr", "mg", "en"] = "fr"


# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------


class SettingIn(PilotageModel):
    value: Any


class WeightSetIn(PilotageModel):
    version: str = Field(min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=500)
    weights: dict[str, Any]


# --------------------------------------------------------------------------
# Laboratoire
# --------------------------------------------------------------------------


class PhysioIn(PilotageModel):
    weight_kg: Decimal = Field(gt=20, le=350)
    height_cm: Decimal = Field(gt=80, le=250)
    birth_date: date
    sex: Sex = Sex.UNSPECIFIED
    activity_level: ActivityLevel = ActivityLevel.SEDENTARY


class ProfilEssaiIn(PilotageModel):
    goal: Goal = Goal.BALANCED_DIET
    kcal_target: Decimal | None = Field(default=None, ge=800, le=6000)
    physio: PhysioIn | None = None
    allergens: list[Allergen] = Field(default_factory=list)
    restrictions: list[RestrictionType] = Field(default_factory=list)
    disliked_ingredients: list[str] = Field(default_factory=list, max_length=50)
    preferred_ingredients: list[str] = Field(default_factory=list, max_length=50)
    favorite_ingredients: list[str] = Field(default_factory=list, max_length=50)
    budget_par_jour: Decimal | None = Field(default=None, ge=0)


class SimulationIn(PilotageModel):
    profil: ProfilEssaiIn
    jours: int = Field(default=3, ge=1, le=21)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    catalogue: Literal["fictif", "reel"] = "fictif"
    #: Jeu de poids brouillon. Absent : le jeu actif en base (ou les défauts).
    poids: dict[str, Any] | None = None
    #: Surcharges de réglages, validées par le registre.
    regles: dict[str, Any] | None = None
    #: Réponse de modèle simulée, à éprouver contre la validation FN-021.
    reponse_llm: Any = None


class ComparaisonIn(PilotageModel):
    base: SimulationIn
    variante_poids: dict[str, Any] | None = None
    variante_seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


__all__ = [
    "AliasIn",
    "ComparaisonIn",
    "PhysioIn",
    "ProfilEssaiIn",
    "RunIn",
    "SettingIn",
    "SimulationIn",
    "SourcePatch",
    "WeightSetIn",
]
