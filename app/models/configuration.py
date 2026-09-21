"""Paramétrage métier stocké en base.

Deux règles de la spécification imposent ces tables plutôt que des constantes
Python :

* **FN-020** — tous les poids et pénalités du score sont configurables en base,
  et le jeu utilisé est enregistré avec chaque génération. Un poids en dur
  transforme chaque réglage en déploiement (piège n° 11 de la grille).
* **FN-006 / FN-024** — le déblocage des périodes de 14 et 21 jours et les
  paramètres de variété sont pilotés par configuration, pas par du code.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk


class ScoringWeightSet(Base, TimestampMixin):
    """FN-020 — jeu de poids versionné.

    `version` est copiée dans `recommendation_runs.scoring_version` à chaque
    génération : c'est ce qui rend une régression de qualité analysable.
    """

    __tablename__ = "scoring_weight_sets"

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    #: Poids **réellement lus** par `app.services.recommandation.scorer` :
    #: `nutrition`, `cost`, `preference`, `variety`, `favorite`. Le registre
    #: `app.services.reglages` refuse toute autre clé.
    #:
    #: La formule FN-020 prévoit d'autres termes (`w_local`, `w_facilite`,
    #: `w_reemploi`, `w_historique`, `p_repetition`, `p_refus`, `p_prix_ancien`
    #: au lot 3, `w_semantique` au lot 4). Tant que `scorer` ne les calcule pas,
    #: les enregistrer ici n'aurait **aucun effet** : ils sont listés comme
    #: « prévus, non lus » par la plateforme de pilotage, pas acceptés.
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # Un seul jeu actif à la fois : la contrainte est portée par la base.
        Index(
            "uq_scoring_weight_sets_active",
            "is_active",
            unique=True,
            postgresql_where="is_active",
        ),
    )


class AppSetting(Base):
    """Paramètres opérationnels modifiables sans déploiement.

    Clés attendues au lot 2 : `max_plan_days`, `min_pool_size`, `max_repeats`,
    `min_gap_days`, `protein_rotation_window`, `meal_distribution`,
    `daily_kcal_tolerance`, `meal_kcal_tolerance`, `llm_monthly_budget`,
    `llm_user_quota`.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(128))


__all__ = ["AppSetting", "ScoringWeightSet"]
