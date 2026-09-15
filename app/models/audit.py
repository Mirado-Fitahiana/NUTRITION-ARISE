"""Journal d'audit (FN-036).

Transposition du modèle déjà éprouvé côté NestJS (`audit-log.entity.ts`).

Deux règles non négociables :

* aucune donnée de santé en clair dans `details` — poids, taille et allergies
  n'y figurent jamais, seulement le fait qu'elles ont changé ;
* aucun secret, token, ni fragment de prompt contenant des données
  personnelles.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pg_enum, uuid_pk
from app.models.enums import AuditResult
from app.models.profile import EXTERNAL_USER_ID_LEN


class AuditAction:
    """Actions journalisées (FN-036). Chaînes stables : elles sont requêtées
    par le tableau de bord de supervision."""

    LOGIN = "login"
    ACCESS_DENIED = "access_denied"

    INGREDIENT_UPDATED = "ingredient.updated"
    INGREDIENT_ALLERGENS_VERIFIED = "ingredient.allergens_verified"

    DISH_CREATED = "dish.created"
    DISH_UPDATED = "dish.updated"
    DISH_VALIDATED = "dish.validated"
    DISH_REJECTED = "dish.rejected"
    DISH_PUBLISHED = "dish.published"
    DISH_ARCHIVED = "dish.archived"

    PROFILE_UPDATED = "profile.updated"
    ALLERGY_CHANGED = "profile.allergy_changed"

    PLAN_GENERATED = "plan.generated"
    PLAN_VALIDATION_REJECTED = "plan.validation_rejected"

    PRICE_RECORDED = "price.recorded"
    PRICE_IMPORTED = "price.imported"
    COLLECTION_STARTED = "collection.started"
    COLLECTION_FAILED = "collection.failed"

    ACCOUNT_DELETED = "account.deleted"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    external_user_id: Mapped[str | None] = mapped_column(String(EXTERNAL_USER_ID_LEN))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[AuditResult] = mapped_column(
        pg_enum(AuditResult, "audit_result"), nullable=False
    )
    #: Détails techniques non sensibles uniquement.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = (
        Index("ix_audit_logs_action_occurred_at", "action", "occurred_at"),
        Index("ix_audit_logs_user_occurred_at", "external_user_id", "occurred_at"),
    )


__all__ = ["AuditAction", "AuditLog"]
