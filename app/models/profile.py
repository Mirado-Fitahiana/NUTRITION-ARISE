"""Profil nutritionnel, préférences, allergies et restrictions (FN-001 → FN-004).

Aucune donnée d'identité ARISE n'est persistée ici : seul l'`external_user_id`
opaque, issu du claim `sub` du JWT, relie ces données à un compte (D-04).
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
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.enums import (
    ActivityLevel,
    Allergen,
    Goal,
    RestrictionType,
    Sex,
)

EXTERNAL_USER_ID_LEN = 128


class NutritionProfile(Base, TimestampMixin):
    """FN-001. Un profil par utilisateur ARISE."""

    __tablename__ = "nutrition_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()

    # `sub` du JWT. Nullable pour permettre l'anonymisation à la suppression de
    # compte (§4.6) sans détruire les statistiques agrégées.
    external_user_id: Mapped[str | None] = mapped_column(
        String(EXTERNAL_USER_ID_LEN), unique=True, index=True
    )

    goal: Mapped[Goal] = mapped_column(pg_enum(Goal, "goal"), nullable=False)
    weight_kg: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    height_cm: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[Sex] = mapped_column(pg_enum(Sex, "sex"), nullable=False)
    activity_level: Mapped[ActivityLevel] = mapped_column(
        pg_enum(ActivityLevel, "activity_level"), nullable=False
    )
    meals_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    household_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Lot 3 — budget et localisation.
    daily_budget: Mapped[float | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="MGA")
    city: Mapped[str | None] = mapped_column(String(120))
    district: Mapped[str | None] = mapped_column(String(120))
    travel_radius_km: Mapped[float | None] = mapped_column(Numeric(6, 2))  # lot 4

    # FN-039 — champs déclaratifs, jamais présélectionnés.
    declared_pregnancy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    declared_breastfeeding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    declared_medical_condition: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    disclaimer_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    targets: Mapped[list["NutritionTarget"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    allergies: Mapped[list["UserAllergy"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    restrictions: Mapped[list["DietaryRestriction"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    preferences: Mapped["DietaryPreference | None"] = relationship(
        back_populates="profile", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        CheckConstraint(
            "weight_kg > 0 AND weight_kg BETWEEN 25 AND 300", name="weight_range"
        ),
        CheckConstraint(
            "height_cm > 0 AND height_cm BETWEEN 100 AND 250", name="height_range"
        ),
        CheckConstraint("meals_per_day BETWEEN 3 AND 5", name="meals_per_day_range"),
        CheckConstraint("household_size >= 1", name="household_size_positive"),
        CheckConstraint(
            "daily_budget IS NULL OR daily_budget >= 0", name="budget_not_negative"
        ),
    )


class NutritionProfileHistory(Base):
    """FN-001 — historisation de toute modification du profil."""

    __tablename__ = "nutrition_profile_history"

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    changed_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    #: Copie du profil AVANT modification.
    previous_values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))


class NutritionTarget(Base):
    """FN-038 — besoins calculés, conservés pour que l'historique reste
    interprétable même après un changement de formule."""

    __tablename__ = "nutrition_targets"

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    bmr: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    tdee: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    kcal_target: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    protein_g: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    carbs_g: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    fat_g: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)

    #: Vrai lorsque la cible a été relevée au plancher de sécurité (FN-038).
    safety_floor_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    formula: Mapped[str] = mapped_column(
        String(64), nullable=False, default="mifflin_st_jeor"
    )
    formula_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="1.0"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    profile: Mapped[NutritionProfile] = relationship(back_populates="targets")

    __table_args__ = (
        CheckConstraint("kcal_target > 0", name="kcal_target_positive"),
        CheckConstraint(
            "protein_g >= 0 AND carbs_g >= 0 AND fat_g >= 0",
            name="macros_not_negative",
        ),
    )


class UserAllergy(Base):
    """FN-003 · 🔴 Contrainte bloquante absolue."""

    __tablename__ = "user_allergies"

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    allergen: Mapped[Allergen] = mapped_column(
        pg_enum(Allergen, "allergen"), nullable=False
    )
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    profile: Mapped[NutritionProfile] = relationship(back_populates="allergies")

    __table_args__ = (UniqueConstraint("profile_id", "allergen"),)


class DietaryRestriction(Base):
    """FN-004. Une restriction personnalisée non normalisée n'est jamais
    appliquée silencieusement : elle est signalée à l'administrateur."""

    __tablename__ = "dietary_restrictions"

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    restriction_type: Mapped[RestrictionType] = mapped_column(
        pg_enum(RestrictionType, "restriction_type"), nullable=False
    )
    #: Libellé saisi par l'utilisateur pour `custom`, `religious`, `forbidden_food`.
    custom_label: Mapped[str | None] = mapped_column(String(255))

    #: Résultat de la normalisation vers des ingrédients et tags connus.
    normalized_ingredient_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    normalized_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    is_normalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    needs_admin_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    profile: Mapped[NutritionProfile] = relationship(back_populates="restrictions")

    __table_args__ = (
        CheckConstraint(
            "restriction_type NOT IN ('custom', 'religious', 'forbidden_food')"
            " OR custom_label IS NOT NULL",
            name="custom_requires_label",
        ),
        # Écran d'administration : restrictions en attente de normalisation.
        Index(
            "ix_dietary_restrictions_pending_review",
            "needs_admin_review",
            postgresql_where="needs_admin_review",
        ),
    )


class DietaryPreference(Base, TimestampMixin):
    """FN-002 — modulateurs de score, jamais bloquants."""

    __tablename__ = "dietary_preferences"

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    prefer_local_products: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    prefer_quick_dishes: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    prefer_easy_dishes: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    prefer_economical_dishes: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    vegetarian_preference: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    preferred_cuisines: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    preferred_protein_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    #: Nombre maximal d'occurrences d'un même aliment sur la période.
    max_food_frequency: Mapped[int | None] = mapped_column(Integer)

    profile: Mapped[NutritionProfile] = relationship(back_populates="preferences")

    __table_args__ = (
        CheckConstraint(
            "max_food_frequency IS NULL OR max_food_frequency > 0",
            name="max_food_frequency_positive",
        ),
    )


class FavoriteIngredient(Base):
    """FN-002 — aliment favori. Bonifie le score, ne filtre rien."""

    __tablename__ = "favorite_ingredients"

    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredients.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DislikedIngredient(Base):
    """FN-002 — aliment refusé. **Exclusion dure**, au même titre qu'une
    restriction : un plat le contenant est éliminé au filtrage (FN-019)."""

    __tablename__ = "disliked_ingredients"

    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredients.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "DietaryPreference",
    "DietaryRestriction",
    "DislikedIngredient",
    "FavoriteIngredient",
    "NutritionProfile",
    "NutritionProfileHistory",
    "NutritionTarget",
    "UserAllergy",
]
