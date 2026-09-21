"""Collecte de prix et exécutions longues (lot 4 conditionnel — FN-013, FN-017, §9.7).

Trois décisions du plan de la plateforme de pilotage (`plan.md`, §5) structurent
ce module :

* **aucune écriture dans `ingredient_prices`.** Les offres extraites restent en
  transit dans `scraped_offers` tant que le lot 4 n'a pas reçu son feu vert.
  Aucun modèle de ce fichier ne référence `IngredientPrice`, et
  `tests/test_collecte_moteur.py` le vérifie ;
* **une exécution longue est générique** (`operation_runs`). La collecte,
  l'indexation vectorielle et les campagnes d'évaluation partagent la même
  progression. La spécification nomme `scraping_runs` (§10.3) : l'écart est
  assumé, et documenté dans le plan (décision 6) ;
* **les événements sont en ajout seul.** L'écran de suivi lit ce qui est arrivé
  depuis son dernier passage, sans jamais tout recharger.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.catalog import ACTOR_ID_LEN
from app.models.enums import OperationStatus

#: Plancher du délai entre deux requêtes vers un même site, en secondes. Il est
#: porté par la base **et** par `app.services.collecte.politesse` : une source
#: mal configurée ne peut pas marteler un site, même si le code l'oubliait.
DELAI_PLANCHER_S = 3


class ScrapingSource(Base, TimestampMixin):
    """Une source de collecte et son état de conformité (FN-013, SPIKE-01).

    Les **sélecteurs** d'extraction ne sont pas ici : ils vivent dans le
    connecteur de la plateforme (`app/services/collecte/connecteurs`). Les
    modifier passe par une revue de code et un test sur instantané, jamais par
    un champ de formulaire qui casserait l'extraction en silence.
    """

    __tablename__ = "scraping_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: `prestashop` ou `woocommerce` : désigne le connecteur utilisé.
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Pages de catalogue à relever. Le mode « structure » n'en lit que la
    #: première : on mesure une structure, pas un catalogue.
    pages: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    delay_seconds: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=DELAI_PLANCHER_S
    )
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    #: Interrupteur : une source désactivée ne peut pas être lancée.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Point de vente auquel les prix seront rattachés le jour où ils seront
    #: promus (P5). Inutilisé tant que le lot 4 n'est pas ouvert.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL")
    )

    #: Critère 4 de SPIKE-01 — les CGU ne se lisent pas par programme : elles
    #: s'attestent, avec un nom et une date.
    tos_attested_by: Mapped[str | None] = mapped_column(String(ACTOR_ID_LEN))
    tos_attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tos_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"delay_seconds >= {DELAI_PLANCHER_S}", name="delay_floor"),
        CheckConstraint("max_pages BETWEEN 1 AND 50", name="max_pages_range"),
        CheckConstraint(
            "platform IN ('prestashop', 'woocommerce')", name="platform_known"
        ),
        CheckConstraint(
            "(tos_attested_by IS NULL AND tos_attested_at IS NULL)"
            " OR (tos_attested_by IS NOT NULL AND tos_attested_at IS NOT NULL)",
            name="tos_attestation_complete",
        ),
    )


class OperationRun(Base):
    """Une exécution longue : collecte, rejeu, indexation, évaluation.

    `subject` désigne ce sur quoi elle porte (le slug d'une source, par
    exemple) : c'est lui qui interdit deux collectes simultanées sur un même
    site.
    """

    __tablename__ = "operation_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: `scraping.structure`, `scraping.collecte`, `scraping.rejeu`…
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[OperationStatus] = mapped_column(
        pg_enum(OperationStatus, "operation_status"),
        nullable=False,
        default=OperationStatus.PENDING,
    )
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # --- Progression, lue par sondage ---------------------------------------
    step: Mapped[str | None] = mapped_column(String(64))
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int | None] = mapped_column(Integer)
    counters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Alerte de changement de structure (FN-013 : plus de 20 % d'échecs).
    alert: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    requested_by: Mapped[str | None] = mapped_column(String(ACTOR_ID_LEN))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("done >= 0", name="done_not_negative"),
        CheckConstraint("total IS NULL OR total >= 0", name="total_not_negative"),
        Index("ix_operation_runs_kind_created_at", "kind", "created_at"),
        Index("ix_operation_runs_subject_status", "subject", "status"),
    )


class OperationRunEvent(Base):
    """Journal de progression d'une exécution, en ajout seul."""

    __tablename__ = "operation_run_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Numéro d'ordre dans l'exécution : l'écran demande « ce qui suit `seq` ».
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    step: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("run_id", "seq"),
        CheckConstraint("level IN ('info', 'warning', 'error')", name="level_known"),
    )


class ScrapingError(Base):
    """FN-013 — une erreur de collecte, **avec son contexte**."""

    __tablename__ = "scraping_errors"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scraping_sources.id", ondelete="SET NULL")
    )
    url: Mapped[str | None] = mapped_column(String(1000))
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ScrapedOffer(Base):
    """Une offre extraite d'une page — **en transit, jamais un prix observé**.

    `price_status` et `match_status` sont indépendants : un prix illisible sur
    un produit bien apparié et un prix lisible sur un libellé inconnu sont deux
    problèmes différents, qui ne se règlent pas au même endroit.
    """

    __tablename__ = "scraped_offers"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scraping_sources.id", ondelete="SET NULL")
    )
    url: Mapped[str | None] = mapped_column(String(1000))
    label_raw: Mapped[str] = mapped_column(String(500), nullable=False)
    label_normalized: Mapped[str] = mapped_column(String(500), nullable=False)

    price_raw: Mapped[str | None] = mapped_column(String(120))
    price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    #: `read`, `absent` (aucun prix affiché) ou `unreadable` (prix présent
    #: mais illisible). Seul `unreadable` compte comme échec d'extraction.
    price_status: Mapped[str] = mapped_column(String(16), nullable=False)
    price_reason: Mapped[str | None] = mapped_column(String(255))

    packaging_raw: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[float | None] = mapped_column(Numeric(10, 3))
    unit: Mapped[str | None] = mapped_column(String(40))
    availability: Mapped[str | None] = mapped_column(String(40))

    ingredient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingredients.id", ondelete="SET NULL")
    )
    #: `matched` ou `unmatched`. Appariement **exact** uniquement.
    match_status: Mapped[str] = mapped_column(String(16), nullable=False)
    match_reason: Mapped[str | None] = mapped_column(String(255))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("price IS NULL OR price > 0", name="price_positive"),
        CheckConstraint(
            "price_status IN ('read', 'absent', 'unreadable')", name="price_status_known"
        ),
        CheckConstraint(
            "match_status IN ('matched', 'unmatched')", name="match_status_known"
        ),
        Index("ix_scraped_offers_label_match", "label_normalized", "match_status"),
    )


__all__ = [
    "DELAI_PLANCHER_S",
    "OperationRun",
    "OperationRunEvent",
    "ScrapedOffer",
    "ScrapingError",
    "ScrapingSource",
]
