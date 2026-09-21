"""Domaine des prix — lot 3 (FN-013 → FN-016, FN-030, D-08 → D-10).

Trois décisions de la spécification structurent ce module :

* **D-08** — l'unité atomique est l'**observation de prix**. La méthode de
  collecte (`manual`, `official`, `community`, `scraping`) n'est qu'un attribut :
  c'est ce qui permettra d'ajouter le scraping sans réécrire le pipeline.
* **D-09** — les prix sont exposés en **fourchette** (min / médiane / max) avec
  date et confiance, jamais en valeur unique. Le marché est négocié et volatil ;
  promettre un prix exact serait une promesse intenable.
* **D-10** — le MVP couvre Antananarivo, mais le schéma est multi-villes dès
  l'origine (`zone` sur le vendeur).

**Une observation ne se modifie jamais.** Corriger un prix, c'est en saisir un
nouveau (FN-015). L'historique est la matière première de l'agrégation : le
raser à chaque correction reviendrait à perdre la volatilité qu'on cherche
précisément à mesurer.

**L'unité est conservée telle qu'observée.** Un prix se relève au kapoaka ou à
la botte, pas en grammes. La conversion passe par `app.services.units`, qui
*bloque* si le facteur n'existe pas — ne pas normaliser à la saisie pour
contourner ce blocage.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.catalog import ACTOR_ID_LEN, Dish, Ingredient
from app.models.enums import PriceSource, VendorType

#: Devise unique du MVP (D-10). Le champ existe pour rester lisible en base et
#: pour ne pas avoir à migrer si une seconde devise apparaît.
DEFAULT_CURRENCY = "MGA"


class Vendor(Base, TimestampMixin):
    """FN-030 — un point de vente, ou le porteur d'une série officielle.

    `type = official_reference` désigne une source publique (INSTAT,
    mercuriale) et non un commerce : la confusion entre un indice régional et
    un prix relevé chez un commerçant précis est le risque nommé du sprint 05.
    """

    __tablename__ = "vendors"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    type: Mapped[VendorType] = mapped_column(
        pg_enum(VendorType, "vendor_type"), nullable=False
    )

    #: Zone de chalandise. `Antananarivo` pour tout le MVP (D-10).
    zone: Mapped[str] = mapped_column(String(120), nullable=False)
    address: Mapped[str | None] = mapped_column(String(300))
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))

    #: Renseigné pour une source scrapée ou officielle (D-08).
    source_url: Mapped[str | None] = mapped_column(String(500))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    prices: Mapped[list["IngredientPrice"]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
            name="latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
            name="longitude_range",
        ),
        Index("ix_vendors_zone_active", "zone", "is_active"),
    )

    def __repr__(self) -> str:  # pragma: no cover - confort de débogage
        return f"<Vendor {self.slug} ({self.type})>"


class IngredientPrice(Base):
    """D-08 — une observation de prix, immuable.

    Pas de `TimestampMixin` : `updated_at` n'aurait aucun sens sur une donnée
    qu'on ne modifie pas. `collected_at` est la date d'**observation** — celle
    qui compte pour la fraîcheur — et `recorded_at` celle de la saisie.

    FN-015 : **un prix sans date d'observation est refusé.** `collected_at` est
    donc `nullable=False` et sans valeur par défaut : rien ne doit pouvoir
    substituer silencieusement la date de saisie à la date de relevé.
    """

    __tablename__ = "ingredient_prices"

    id: Mapped[uuid.UUID] = uuid_pk()
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )

    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default=DEFAULT_CURRENCY
    )
    #: Unité **telle qu'observée** au point de vente (kapoaka, botte, kg…).
    #: Jamais normalisée à la saisie : la conversion appartient à `units.py`.
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Quantité vendue pour ce prix. 1 kapoaka, 2 bottes…
    quantity: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False, default=1)

    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    source: Mapped[PriceSource] = mapped_column(
        pg_enum(PriceSource, "price_source"), nullable=False
    )
    #: `sub` du contributeur (D-04) : jamais de clé étrangère vers ARISE.
    collected_by: Mapped[str | None] = mapped_column(String(ACTOR_ID_LEN))

    #: Score 0–1 attribué à la saisie (FN-015). Calculé par `services.pricing`.
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    #: Renseigné quand la saisie s'écartait de plus de 50 % de la médiane
    #: connue et que le contributeur a confirmé (FN-015).
    outlier_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    note: Mapped[str | None] = mapped_column(String(255))

    ingredient: Mapped[Ingredient] = relationship()
    vendor: Mapped[Vendor] = relationship(back_populates="prices")

    __table_args__ = (
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "confidence IS NULL OR (confidence BETWEEN 0 AND 1)",
            name="confidence_range",
        ),
        # Une même personne relevant deux fois le même prix au même instant
        # chez le même vendeur est un doublon de saisie, pas une observation.
        UniqueConstraint(
            "ingredient_id",
            "vendor_id",
            "collected_at",
            "unit",
            "source",
            name="uq_ingredient_prices_observation",
        ),
        Index("ix_ingredient_prices_lookup", "ingredient_id", "collected_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - confort de débogage
        return f"<IngredientPrice {self.price} {self.currency}/{self.unit}>"


class DishCostEstimate(Base):
    """FN-017 — coût d'un plat, **dérivé** et recalculé (lot 3, sprint 06).

    Comme les valeurs nutritionnelles (D-11), ce chiffrage ne se saisit pas.
    `coverage_ratio` dit quelle part des ingrédients a réellement un prix : un
    coût calculé sur 40 % des ingrédients n'est pas un coût, et l'exposer sans
    ce ratio serait mensonger.
    """

    __tablename__ = "dish_cost_estimates"

    id: Mapped[uuid.UUID] = uuid_pk()
    dish_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dishes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: `NULL` = moyenne toutes zones confondues.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), index=True
    )

    total_cost_min: Mapped[float | None] = mapped_column(Numeric(12, 2))
    total_cost_median: Mapped[float | None] = mapped_column(Numeric(12, 2))
    total_cost_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default=DEFAULT_CURRENCY
    )

    #: Part des ingrédients du plat effectivement chiffrés, entre 0 et 1.
    coverage_ratio: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    #: Observation la plus ancienne ayant servi au calcul : c'est elle qui
    #: détermine la fraîcheur de l'ensemble.
    oldest_observation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    dish: Mapped[Dish] = relationship()
    vendor: Mapped[Vendor | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "coverage_ratio BETWEEN 0 AND 1",
            name="coverage_range",
        ),
        CheckConstraint(
            "total_cost_min IS NULL OR total_cost_median IS NULL "
            "OR total_cost_min <= total_cost_median",
            name="min_le_median",
        ),
        CheckConstraint(
            "total_cost_median IS NULL OR total_cost_max IS NULL "
            "OR total_cost_median <= total_cost_max",
            name="median_le_max",
        ),
        UniqueConstraint("dish_id", "vendor_id", name="uq_dish_cost_estimates_scope"),
    )


__all__ = ["DEFAULT_CURRENCY", "DishCostEstimate", "IngredientPrice", "Vendor"]
