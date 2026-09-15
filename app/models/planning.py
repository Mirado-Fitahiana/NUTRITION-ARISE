"""Programmes alimentaires, suivi et feedback (FN-021 → FN-025, FN-031 → FN-033).

Le schéma du lot 2 est migré dès le lot 1 (critère de fin du lot 1), afin que la
première migration soit aussi la dernière migration structurelle avant le
générateur.

Invariant central — **le figement** (FN-023) : `meal_plan_meals.dish_snapshot`
contient une copie complète du plat au moment de la génération. Sans elle,
archiver un plat réécrirait rétroactivement l'historique de tous les
utilisateurs.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.enums import (
    CostConfidence,
    FeedbackType,
    Goal,
    JobStatus,
    MealPlanStatus,
    MealSlot,
    TrackedStatus,
    ValidationOutcome,
)
from app.models.profile import EXTERNAL_USER_ID_LEN


class MealPlan(Base, TimestampMixin):
    """FN-023 — un programme alimentaire délivré à un utilisateur."""

    __tablename__ = "meal_plans"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: Mis à NULL à la suppression du compte ARISE : le programme est anonymisé,
    #: jamais supprimé (§4.6).
    external_user_id: Mapped[str | None] = mapped_column(
        String(EXTERNAL_USER_ID_LEN), index=True
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="SET NULL")
    )

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    days_count: Mapped[int] = mapped_column(Integer, nullable=False)
    household_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    goal: Mapped[Goal] = mapped_column(pg_enum(Goal, "goal"), nullable=False)
    kcal_target: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)

    status: Mapped[MealPlanStatus] = mapped_column(
        pg_enum(MealPlanStatus, "meal_plan_status"),
        nullable=False,
        default=MealPlanStatus.GENERATING,
    )

    # Coût toujours exprimé en fourchette, avec son niveau de fiabilité (D-09).
    total_cost_min: Mapped[float | None] = mapped_column(Numeric(12, 2))
    total_cost_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    cost_confidence: Mapped[CostConfidence] = mapped_column(
        pg_enum(CostConfidence, "cost_confidence"),
        nullable=False,
        default=CostConfidence.ESTIMATED,
    )

    #: FN-025 — une regénération crée une nouvelle version, elle n'écrase rien.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meal_plans.id", ondelete="SET NULL")
    )

    days: Mapped[list["MealPlanDay"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="MealPlanDay.day_index",
    )

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="period_ordered"),
        CheckConstraint("days_count > 0", name="days_count_positive"),
        CheckConstraint("household_size >= 1", name="household_size_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "total_cost_min IS NULL OR total_cost_max IS NULL"
            " OR total_cost_max >= total_cost_min",
            name="cost_range_ordered",
        ),
        Index("ix_meal_plans_user_status", "external_user_id", "status"),
    )


class MealPlanDay(Base):
    """FN-023 — une journée du programme. Tous les jours sont couverts."""

    __tablename__ = "meal_plan_days"

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meal_plans.id", ondelete="CASCADE"), index=True, nullable=False
    )
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    day_date: Mapped[date] = mapped_column(Date, nullable=False)

    kcal_total: Mapped[float | None] = mapped_column(Numeric(8, 2))
    protein_total: Mapped[float | None] = mapped_column(Numeric(8, 2))
    carbs_total: Mapped[float | None] = mapped_column(Numeric(8, 2))
    fat_total: Mapped[float | None] = mapped_column(Numeric(8, 2))
    #: FN-023 — conseil du jour, optionnel.
    daily_tip: Mapped[str | None] = mapped_column(Text)

    plan: Mapped[MealPlan] = relationship(back_populates="days")
    meals: Mapped[list["MealPlanMeal"]] = relationship(
        back_populates="day", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "day_index"),
        CheckConstraint("day_index >= 0", name="day_index_not_negative"),
    )


class MealPlanMeal(Base):
    """FN-023 — un repas figé.

    `dish_snapshot` est la source d'affichage ; `dish_id` n'est conservé qu'à
    titre indicatif et passe à NULL si le plat disparaît, sans altérer le repas.
    """

    __tablename__ = "meal_plan_meals"

    id: Mapped[uuid.UUID] = uuid_pk()
    day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meal_plan_days.id", ondelete="CASCADE"), index=True, nullable=False
    )
    slot: Mapped[MealSlot] = mapped_column(pg_enum(MealSlot, "meal_slot"), nullable=False)

    dish_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dishes.id", ondelete="SET NULL")
    )
    #: 🔴 COPIE FIGÉE et complète du plat : nom, description, ingrédients,
    #: quantités, étapes, valeurs nutritionnelles, allergènes, coût.
    dish_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    servings: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=1)
    kcal: Mapped[float | None] = mapped_column(Numeric(8, 2))
    protein_g: Mapped[float | None] = mapped_column(Numeric(8, 2))
    carbs_g: Mapped[float | None] = mapped_column(Numeric(8, 2))
    fat_g: Mapped[float | None] = mapped_column(Numeric(8, 2))
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 2))

    #: Texte rédigé par le LLM (FN-021), ou repli déterministe.
    justification: Mapped[str | None] = mapped_column(Text)
    tracked_status: Mapped[TrackedStatus] = mapped_column(
        pg_enum(TrackedStatus, "tracked_status"),
        nullable=False,
        default=TrackedStatus.PENDING,
    )
    consumed_quantity: Mapped[float | None] = mapped_column(Numeric(6, 2))

    day: Mapped[MealPlanDay] = relationship(back_populates="meals")
    feedback: Mapped[list["UserMealFeedback"]] = relationship(
        back_populates="meal", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("day_id", "slot"),
        CheckConstraint("servings > 0", name="servings_positive"),
        CheckConstraint("dish_snapshot <> '{}'::jsonb", name="snapshot_not_empty"),
    )


class GenerationJob(Base):
    """FN-023 — génération asynchrone au-delà de 5 s (`202 Accepted`)."""

    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    external_user_id: Mapped[str | None] = mapped_column(
        String(EXTERNAL_USER_ID_LEN), index=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meal_plans.id", ondelete="SET NULL")
    )
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), nullable=False, default=JobStatus.PENDING
    )
    request_params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Code stable de FN-037 (`NO_COMPATIBLE_DISH`, `MEDICAL_LIMIT`, …).
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecommendationRun(Base):
    """FN-020 / FN-021 — trace d'exécution d'une génération.

    Sans `scoring_version` ni `random_seed`, une régression de qualité est
    indébuggable et une génération n'est pas reproductible.
    """

    __tablename__ = "recommendation_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meal_plans.id", ondelete="CASCADE"), index=True, nullable=False
    )

    scoring_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(120))

    #: D-07 — **un seul appel LLM par programme**. La contrainte ci-dessous rend
    #: la dérive vers un appel par repas impossible, pas seulement improbable.
    llm_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    #: Vrai lorsque le repli textuel a été utilisé : le fournisseur IA ne doit
    #: jamais bloquer une génération (FN-037).
    used_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    random_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("llm_call_count <= 1", name="single_llm_call_per_plan"),
        CheckConstraint("cost >= 0", name="cost_not_negative"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0", name="tokens_not_negative"
        ),
    )


class RecommendationCandidate(Base):
    """FN-020 — candidats évalués pour un créneau, avec le détail du score.

    Conservé pour expliquer une sélection et calibrer les poids ; c'est la seule
    manière de comprendre a posteriori pourquoi un plat a été retenu.
    """

    __tablename__ = "recommendation_candidates"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    slot: Mapped[MealSlot] = mapped_column(pg_enum(MealSlot, "meal_slot"), nullable=False)
    dish_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dishes.id", ondelete="SET NULL")
    )
    score: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    #: Contribution de chaque terme de la formule FN-020.
    score_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_recommendation_candidates_run_slot", "run_id", "day_index", "slot"),)


class PlanValidation(Base):
    """FN-022 · 🔴 Journal de la validation finale.

    Exécutée deux fois : après sélection, puis après rédaction LLM. Un échec sur
    les contrôles 1 à 4 est un incident de sécurité alimentaire, pas un log :
    `is_food_safety_incident` alimente le compteur du tableau de bord (FN-035),
    qui doit rester à zéro.
    """

    __tablename__ = "plan_validations"

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meal_plans.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: `post_selection` ou `post_llm`.
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[ValidationOutcome] = mapped_column(
        pg_enum(ValidationOutcome, "validation_outcome"), nullable=False
    )
    #: Numéros des contrôles FN-022 en échec (1 à 11).
    failed_checks: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list
    )
    corrections: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_food_safety_incident: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "phase IN ('post_selection', 'post_llm')", name="phase_known"
        ),
        CheckConstraint(
            "failed_checks <@ ARRAY[1,2,3,4,5,6,7,8,9,10,11]::integer[]",
            name="failed_checks_known",
        ),
        Index(
            "ix_plan_validations_incidents",
            "is_food_safety_incident",
            postgresql_where="is_food_safety_incident",
        ),
    )


class UserMealFeedback(Base):
    """FN-032. Un feedback `allergy_issue` est un incident : alerte immédiate et
    mise en revue du plat concerné."""

    __tablename__ = "user_meal_feedback"

    id: Mapped[uuid.UUID] = uuid_pk()
    meal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meal_plan_meals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    feedback_type: Mapped[FeedbackType] = mapped_column(
        pg_enum(FeedbackType, "feedback_type"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    meal: Mapped[MealPlanMeal] = relationship(back_populates="feedback")

    __table_args__ = (
        Index(
            "ix_user_meal_feedback_allergy",
            "feedback_type",
            postgresql_where="feedback_type = 'allergy_issue'",
        ),
    )


__all__ = [
    "GenerationJob",
    "MealPlan",
    "MealPlanDay",
    "MealPlanMeal",
    "PlanValidation",
    "RecommendationCandidate",
    "RecommendationRun",
    "UserMealFeedback",
]
