"""Contrats HTTP de la génération de programme (FN-023, sprint 09).

Aucun schéma d'entrée ne porte d'identifiant utilisateur : l'utilisateur vient
du claim `sub`, toujours. C'est la règle structurelle du service, et
`tests/test_isolation.py` la fait respecter sur toute route ajoutée.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Goal, JobStatus, MealPlanStatus, MealSlot


class PlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class GenerateIn(PlanningModel):
    """Demande de génération. `days` vaut 1 par défaut — le MVP vise 7."""

    goal: Goal
    days: int = Field(default=1, ge=1, le=21)
    budget_mga: Decimal | None = Field(default=None, ge=0)
    preferred_vendor_type: str | None = Field(default=None, max_length=40)
    #: Rejouer une génération à l'identique (débogage). Jamais exposé au mobile.
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class JobAccepted(OutModel):
    """202 Accepted — le travail est lancé, pas terminé."""

    job_id: uuid.UUID
    status: JobStatus
    #: Intervalle de sondage conseillé, aligné sur le paiement MVola côté mobile.
    poll_after_seconds: int = 3


class MealOut(OutModel):
    slot: MealSlot
    dish_slug: str | None
    name: str
    kcal: Decimal | None
    protein_g: Decimal | None
    carbs_g: Decimal | None
    fat_g: Decimal | None
    estimated_cost: Decimal | None
    justification: str | None


class DayOut(OutModel):
    day_index: int
    day_date: date
    kcal_total: Decimal | None
    meals: list[MealOut]


class PlanOut(OutModel):
    id: uuid.UUID
    status: MealPlanStatus
    goal: Goal
    start_date: date
    end_date: date
    days_count: int
    kcal_target: Decimal
    total_cost_min: Decimal | None
    total_cost_max: Decimal | None
    cost_confidence: str
    version: int
    days: list[DayOut]
    #: Créneaux qu'aucun plat n'a pu couvrir. Signalés, jamais masqués.
    uncovered_slots: list[str] = Field(default_factory=list)


class JobOut(OutModel):
    """Suivi du job. Porte le plan dès qu'il existe, et le motif s'il a échoué."""

    job_id: uuid.UUID
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_detail: str | None
    plan: PlanOut | None


class PlanSummary(OutModel):
    id: uuid.UUID
    status: MealPlanStatus
    goal: Goal
    start_date: date
    end_date: date
    days_count: int
    kcal_target: Decimal
    version: int
    created_at: datetime


class PlanPage(OutModel):
    total: int
    items: list[PlanSummary]


__all__ = [
    "DayOut",
    "GenerateIn",
    "JobAccepted",
    "JobOut",
    "MealOut",
    "PlanOut",
    "PlanPage",
    "PlanSummary",
]
