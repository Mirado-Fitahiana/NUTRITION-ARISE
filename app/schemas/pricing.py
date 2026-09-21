"""Contrats HTTP du domaine des prix (lot 3).

Comme pour le catalogue, `extra="forbid"` : une faute de frappe dans un nom de
champ doit échouer, pas se perdre silencieusement.

Deux règles se lisent dans les types eux-mêmes :

* **D-09** — aucune sortie n'expose un prix unique. `PriceRangeOut` est le seul
  format de restitution, et il porte toujours sa date, sa fraîcheur et sa
  confiance.
* **FN-015** — `collected_at` est **obligatoire** en entrée. Un prix sans date
  d'observation n'est pas un prix.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import PriceFreshness, PriceSource, VendorType


class PricingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------
# Vendeurs
# --------------------------------------------------------------------------


class VendorIn(PricingModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
    name: str = Field(min_length=1, max_length=200)
    type: VendorType
    zone: str = Field(min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    source_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    notes: str | None = None


class VendorPatch(PricingModel):
    """Mise à jour partielle. Le `slug` n'y figure pas : c'est la clé
    fonctionnelle, elle ne change jamais."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: VendorType | None = None
    zone: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    source_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    notes: str | None = None


class VendorOut(OutModel):
    id: uuid.UUID
    slug: str
    name: str
    type: VendorType
    zone: str
    address: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    source_url: str | None
    is_active: bool
    notes: str | None


class VendorPage(OutModel):
    total: int
    items: list[VendorOut]


# --------------------------------------------------------------------------
# Observations de prix
# --------------------------------------------------------------------------


class PriceIn(PricingModel):
    """FN-015 — une observation. Elle ne se modifie pas : pour corriger, on en
    saisit une nouvelle."""

    ingredient_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    vendor_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    price: Decimal = Field(gt=0, le=Decimal("99999999.99"))
    #: Unité **telle qu'observée**, jamais normalisée à la saisie.
    unit: str = Field(min_length=1, max_length=40)
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    currency: str = Field(default="MGA", pattern=r"^[A-Z]{3}$")
    #: Obligatoire (FN-015). Pas de valeur par défaut : la date de saisie ne
    #: doit jamais pouvoir se substituer à la date de relevé.
    collected_at: datetime
    source: PriceSource = PriceSource.MANUAL
    note: str | None = Field(default=None, max_length=255)
    #: FN-015 — mis à `true` par le contributeur quand il confirme un prix
    #: s'écartant de plus de 50 % de la médiane connue.
    confirm_outlier: bool = False

    @field_validator("collected_at")
    @classmethod
    def _exige_un_fuseau(cls, value: datetime) -> datetime:
        """Une date sans fuseau est ambiguë : à Antananarivo (UTC+3), elle peut
        décaler un relevé d'un jour et donc basculer sa fraîcheur."""
        if value.tzinfo is None:
            raise ValueError("collected_at doit porter un fuseau horaire")
        return value


class PriceBulkIn(PricingModel):
    """Saisie en lot : un opérateur revenant du marché avec 30 prix ne doit pas
    remplir 30 formulaires."""

    observations: list[PriceIn] = Field(min_length=1, max_length=500)
    dry_run: bool = False


class PriceOut(OutModel):
    id: uuid.UUID
    ingredient_id: uuid.UUID
    vendor_id: uuid.UUID
    price: Decimal
    currency: str
    unit: str
    quantity: Decimal
    collected_at: datetime
    recorded_at: datetime
    source: PriceSource
    collected_by: str | None
    confidence: Decimal | None
    outlier_confirmed: bool
    note: str | None
    freshness: PriceFreshness


class PricePage(OutModel):
    total: int
    items: list[PriceOut]


class RejectedObservation(OutModel):
    """Une ligne refusée, avec son motif — jamais un échec silencieux."""

    index: int
    ingredient_slug: str
    vendor_slug: str
    reason: str


class PriceBulkResult(OutModel):
    dry_run: bool
    received: int
    accepted: int
    rejected: list[RejectedObservation]
    #: FN-015 — saisies acceptées mais signalées comme aberrantes.
    flagged_outliers: list[RejectedObservation]


# --------------------------------------------------------------------------
# Restitution (D-09)
# --------------------------------------------------------------------------


class PriceRangeOut(OutModel):
    """D-09 — le seul format sous lequel un prix sort du service.

    `sources` expose **d'où vient le chiffre**. Sans cette ventilation, un
    indice régional officiel et un relevé chez un commerçant précis se
    présenteraient à l'identique — la confusion que le lot 3 nomme comme son
    risque principal.
    """

    ingredient_slug: str
    unit: str
    currency: str
    price_min: Decimal
    price_median: Decimal
    price_max: Decimal
    observed_at: datetime
    freshness: PriceFreshness
    confidence: Decimal
    observations: int
    usable_for_budget: bool
    #: Nombre d'observations par méthode de collecte (D-08).
    sources: dict[PriceSource, int]


class CoverageRow(OutModel):
    """Une ligne de l'écran qui pilote le travail de l'opérateur."""

    ingredient_slug: str
    ingredient_name: str
    observations: int
    last_collected_at: datetime | None
    freshness: PriceFreshness
    vendors: int


class CoverageOut(OutModel):
    """FN-016 — vue de couverture : ce qui manque, et ce qui a vieilli."""

    ingredients_total: int
    ingredients_priced: int
    ingredients_missing: int
    ingredients_obsolete: int
    coverage_ratio: float
    generated_at: datetime
    rows: list[CoverageRow]


# --------------------------------------------------------------------------
# FN-017 — coût des plats
# --------------------------------------------------------------------------


class RetainedScopeOut(OutModel):
    """D'où sort le chiffre — l'utilisateur doit pouvoir le comprendre."""

    scope: str
    vendor_slug: str | None
    observed_at: datetime
    source: PriceSource
    observations: int


class IngredientCostOut(OutModel):
    ingredient_slug: str
    cost_min: Decimal | None
    cost_median: Decimal | None
    cost_max: Decimal | None
    freshness: PriceFreshness
    retained: RetainedScopeOut | None
    #: Motif d'absence. Une ligne non chiffrée ne sort **jamais** sans lui.
    reason: str | None


class DishCostOut(OutModel):
    """FN-017 — coût d'un plat et, surtout, ce qui n'est pas connu.

    `coverage_ratio` et `missing_ingredients` ne sont pas optionnels : c'est ce
    qui empêche de lire le coût de cinq ingrédients sur huit comme le coût du
    plat.
    """

    dish_slug: str
    status: str
    coverage_ratio: Decimal
    currency: str
    total_cost_min: Decimal | None
    total_cost_median: Decimal | None
    total_cost_max: Decimal | None
    portion_cost_min: Decimal | None
    portion_cost_median: Decimal | None
    portion_cost_max: Decimal | None
    oldest_observation_at: datetime | None
    freshness: PriceFreshness
    missing_ingredients: list[str]
    lines: list[IngredientCostOut]


class BasketIn(PricingModel):
    dish_slugs: list[str] = Field(min_length=1, max_length=60)
    vendor_slug: str | None = None
    zone: str | None = None
    #: Nombre de portions voulues par plat, s'il diffère de la recette.
    servings: int | None = Field(default=None, gt=0, le=50)


class BasketOut(OutModel):
    currency: str
    total_cost_min: Decimal | None
    total_cost_median: Decimal | None
    total_cost_max: Decimal | None
    #: Plats dont le coût n'a pas pu être établi : ils ne sont **pas** comptés
    #: dans le total, et sont nommés pour que l'absence soit visible.
    unpriced_dishes: list[str]
    coverage_ratio: Decimal
    freshness: PriceFreshness
    dishes: list[DishCostOut]


__all__ = [
    "BasketIn",
    "BasketOut",
    "CoverageOut",
    "CoverageRow",
    "DishCostOut",
    "IngredientCostOut",
    "RetainedScopeOut",
    "PriceBulkIn",
    "PriceBulkResult",
    "PriceIn",
    "PriceOut",
    "PricePage",
    "PriceRangeOut",
    "RejectedObservation",
    "VendorIn",
    "VendorOut",
    "VendorPage",
    "VendorPatch",
]
