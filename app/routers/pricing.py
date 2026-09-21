"""Points de vente et relevés de prix (lot 3 — FN-015, FN-016, FN-030, D-08 → D-10).

Puisque la saisie manuelle est la source primaire — *« le seul mécanisme garanti
de fonctionner »* (FN-015) — **l'interface de saisie est le livrable central de
ce lot**, pas un accessoire. Trois choix en découlent :

* **la saisie en lot est un point d'entrée de premier rang**, pas une option :
  un opérateur qui revient du marché avec trente prix ne remplit pas trente
  formulaires ;
* **la vue de couverture** (`/ingredient-prices/coverage`) est l'écran qui
  pilote le travail quotidien : elle dit quoi relever, et ce qui a vieilli ;
* **rien n'échoue en silence** : une ligne refusée l'est avec son motif et son
  index, et le `dry_run` permet de tout vérifier avant d'écrire.

Une observation **ne se modifie ni ne se supprime** : il n'y a donc ni `PUT` ni
`DELETE` sur `/ingredient-prices`. Corriger un prix, c'est en saisir un nouveau
(FN-015) — l'historique est la matière première de l'agrégation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Principal, current_user, require_catalog_editor
from app.core.database import get_session
from app.core.errors import ErrorCode, NutritionError
from app.models.catalog import Dish, DishIngredient, Ingredient
from app.models.enums import PriceFreshness, PriceSource
from app.models.pricing import DEFAULT_CURRENCY, IngredientPrice, Vendor
from app.schemas.pricing import (
    BasketIn,
    BasketOut,
    CoverageOut,
    CoverageRow,
    DishCostOut,
    IngredientCostOut,
    PriceBulkIn,
    PriceBulkResult,
    PriceIn,
    PriceOut,
    PricePage,
    PriceRangeOut,
    RejectedObservation,
    RetainedScopeOut,
    VendorIn,
    VendorOut,
    VendorPage,
    VendorPatch,
)
from app.services import audit
from app.services.audit import AuditAction
from app.services.pricing import (
    CoutPlat,
    Observation,
    ReleveCandidat,
    agreger,
    confiance,
    cout_ingredient,
    cout_plat,
    est_aberrant,
    fraicheur,
)
from app.services.units import IngredientConversion

public = APIRouter(prefix="/api/v1", tags=["prix"])
admin = APIRouter(
    prefix="/api/v1/admin",
    tags=["prix (administration)"],
    dependencies=[Depends(require_catalog_editor)],
)


# --------------------------------------------------------------------------
# Sérialisation
# --------------------------------------------------------------------------


def _price_out(row: IngredientPrice, *, maintenant: datetime) -> PriceOut:
    return PriceOut(
        id=row.id,
        ingredient_id=row.ingredient_id,
        vendor_id=row.vendor_id,
        price=row.price,
        currency=row.currency,
        unit=row.unit,
        quantity=row.quantity,
        collected_at=row.collected_at,
        recorded_at=row.recorded_at,
        source=row.source,
        collected_by=row.collected_by,
        confidence=row.confidence,
        outlier_confirmed=row.outlier_confirmed,
        note=row.note,
        freshness=fraicheur(row.collected_at, maintenant=maintenant),
    )


async def _vendor_par_slug(session: AsyncSession, slug: str) -> Vendor:
    row = await session.scalar(select(Vendor).where(Vendor.slug == slug))
    if row is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"Aucun point de vente « {slug} ».",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return row


async def _ingredient_par_slug(session: AsyncSession, slug: str) -> Ingredient:
    row = await session.scalar(select(Ingredient).where(Ingredient.slug == slug))
    if row is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"Aucun ingrédient « {slug} ».",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return row


# --------------------------------------------------------------------------
# Points de vente
# --------------------------------------------------------------------------


@admin.get("/vendors", response_model=VendorPage, summary="FN-030 — points de vente")
async def list_vendors(
    zone: str | None = Query(default=None),
    active_only: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> VendorPage:
    query = select(Vendor)
    if zone:
        query = query.where(Vendor.zone == zone)
    if active_only:
        query = query.where(Vendor.is_active.is_(True))

    total = await session.scalar(
        select(func.count()).select_from(query.subquery())
    )
    rows = await session.scalars(query.order_by(Vendor.name).limit(limit).offset(offset))
    return VendorPage(total=total or 0, items=[VendorOut.model_validate(r) for r in rows])


@admin.post(
    "/vendors",
    response_model=VendorOut,
    status_code=status.HTTP_201_CREATED,
    summary="FN-030 — créer un point de vente",
)
async def create_vendor(
    payload: VendorIn,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> VendorOut:
    if await session.scalar(select(Vendor).where(Vendor.slug == payload.slug)):
        raise NutritionError(
            ErrorCode.CONFLICT,
            message=f"Un point de vente porte déjà le slug « {payload.slug} ».",
            http_status=status.HTTP_409_CONFLICT,
        )

    row = Vendor(id=uuid.uuid4(), **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        AuditAction.VENDOR_CREATED,
        external_user_id=principal.external_user_id,
        resource_type="vendor",
        resource_id=row.id,
        details={"slug": row.slug, "type": str(row.type), "zone": row.zone},
    )
    await session.commit()
    return VendorOut.model_validate(row)


@admin.patch(
    "/vendors/{slug}", response_model=VendorOut, summary="FN-030 — modifier un point de vente"
)
async def update_vendor(
    slug: str,
    payload: VendorPatch,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> VendorOut:
    row = await _vendor_par_slug(session, slug)
    modifie = payload.model_dump(exclude_unset=True)
    for champ, valeur in modifie.items():
        setattr(row, champ, valeur)

    await audit.record(
        session,
        AuditAction.VENDOR_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="vendor",
        resource_id=row.id,
        details={"slug": row.slug, "champs": sorted(modifie)},
    )
    await session.commit()
    return VendorOut.model_validate(row)


# --------------------------------------------------------------------------
# Relevés de prix
# --------------------------------------------------------------------------


async def _mediane_connue(
    session: AsyncSession, ingredient_id: uuid.UUID, unit: str
) -> Decimal | None:
    """Médiane des observations non obsolètes, pour le contrôle d'aberration."""
    rows = await session.scalars(
        select(IngredientPrice).where(
            IngredientPrice.ingredient_id == ingredient_id,
            IngredientPrice.unit == unit,
        )
    )
    fourchette = agreger(
        [
            Observation(price=r.price, collected_at=r.collected_at, source=r.source, unit=r.unit)
            for r in rows
        ]
    )
    return fourchette.mediane if fourchette else None


@admin.get(
    "/ingredient-prices", response_model=PricePage, summary="FN-015 — relevés saisis"
)
async def list_prices(
    ingredient_slug: str | None = Query(default=None),
    vendor_slug: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> PricePage:
    query = select(IngredientPrice)
    if ingredient_slug:
        ingredient = await _ingredient_par_slug(session, ingredient_slug)
        query = query.where(IngredientPrice.ingredient_id == ingredient.id)
    if vendor_slug:
        vendor = await _vendor_par_slug(session, vendor_slug)
        query = query.where(IngredientPrice.vendor_id == vendor.id)

    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(
        query.order_by(IngredientPrice.collected_at.desc()).limit(limit).offset(offset)
    )
    maintenant = datetime.now(UTC)
    return PricePage(
        total=total or 0, items=[_price_out(r, maintenant=maintenant) for r in rows]
    )


@admin.post(
    "/ingredient-prices",
    response_model=PriceBulkResult,
    status_code=status.HTTP_201_CREATED,
    summary="FN-015 — saisir des relevés (unitaire ou en lot)",
)
async def record_prices(
    payload: PriceBulkIn,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> PriceBulkResult:
    """Valide tout, puis écrit. `dry_run` s'arrête après la validation.

    Une ligne refusée n'interrompt pas le lot : elle est rapportée avec son
    index et son motif. L'opérateur corrige et rejoue — plutôt que de perdre
    vingt-neuf saisies valides à cause d'une trentième.
    """
    maintenant = datetime.now(UTC)
    rejets: list[RejectedObservation] = []
    signales: list[RejectedObservation] = []
    a_ecrire: list[IngredientPrice] = []

    for index, obs in enumerate(payload.observations):
        try:
            ingredient = await _ingredient_par_slug(session, obs.ingredient_slug)
            vendor = await _vendor_par_slug(session, obs.vendor_slug)
        except NutritionError as exc:
            rejets.append(
                RejectedObservation(
                    index=index,
                    ingredient_slug=obs.ingredient_slug,
                    vendor_slug=obs.vendor_slug,
                    reason=exc.message,
                )
            )
            continue

        if not vendor.is_active:
            rejets.append(
                RejectedObservation(
                    index=index,
                    ingredient_slug=obs.ingredient_slug,
                    vendor_slug=obs.vendor_slug,
                    reason="le point de vente est inactif",
                )
            )
            continue

        # FN-015 — l'écart de plus de 50 % ne refuse pas la saisie, il la
        # signale. Un prix aberrant est parfois le vrai prix.
        mediane = await _mediane_connue(session, ingredient.id, obs.unit)
        aberrant = est_aberrant(obs.price, mediane)
        if aberrant and not obs.confirm_outlier:
            rejets.append(
                RejectedObservation(
                    index=index,
                    ingredient_slug=obs.ingredient_slug,
                    vendor_slug=obs.vendor_slug,
                    reason=(
                        f"écart de plus de 50 % avec la médiane connue ({mediane}) — "
                        "renvoyer avec confirm_outlier=true pour confirmer"
                    ),
                )
            )
            continue
        if aberrant:
            signales.append(
                RejectedObservation(
                    index=index,
                    ingredient_slug=obs.ingredient_slug,
                    vendor_slug=obs.vendor_slug,
                    reason=f"confirmé malgré un écart avec la médiane ({mediane})",
                )
            )

        a_ecrire.append(
            IngredientPrice(
                id=uuid.uuid4(),
                ingredient_id=ingredient.id,
                vendor_id=vendor.id,
                price=obs.price,
                currency=obs.currency,
                unit=obs.unit,
                quantity=obs.quantity,
                collected_at=obs.collected_at,
                recorded_at=maintenant,
                source=obs.source,
                collected_by=principal.external_user_id,
                confidence=confiance(
                    [
                        Observation(
                            price=obs.price,
                            collected_at=obs.collected_at,
                            source=obs.source,
                            unit=obs.unit,
                        )
                    ],
                    maintenant=maintenant,
                ),
                outlier_confirmed=aberrant,
                note=obs.note,
            )
        )

    if payload.dry_run:
        return PriceBulkResult(
            dry_run=True,
            received=len(payload.observations),
            accepted=len(a_ecrire),
            rejected=rejets,
            flagged_outliers=signales,
        )

    for row in a_ecrire:
        session.add(row)

    if a_ecrire:
        await audit.record(
            session,
            AuditAction.PRICE_RECORDED,
            external_user_id=principal.external_user_id,
            resource_type="ingredient_price",
            details={
                "observations": len(a_ecrire),
                "rejets": len(rejets),
                "aberrations_confirmees": len(signales),
            },
        )
    await session.commit()

    return PriceBulkResult(
        dry_run=False,
        received=len(payload.observations),
        accepted=len(a_ecrire),
        rejected=rejets,
        flagged_outliers=signales,
    )


@admin.get(
    "/ingredient-prices/coverage",
    response_model=CoverageOut,
    summary="FN-016 — ce qui manque et ce qui a vieilli",
)
async def price_coverage(
    missing_only: bool = Query(
        default=False, description="ne lister que les ingrédients sans aucun prix"
    ),
    session: AsyncSession = Depends(get_session),
) -> CoverageOut:
    """L'écran qui pilote le travail de l'opérateur au quotidien.

    Un ingrédient sans prix et un ingrédient au prix obsolète demandent la même
    action — aller relever — et figurent donc dans la même liste.
    """
    maintenant = datetime.now(UTC)

    ingredients = list(await session.scalars(select(Ingredient).order_by(Ingredient.name)))
    agregats = (
        await session.execute(
            select(
                IngredientPrice.ingredient_id,
                func.count(IngredientPrice.id),
                func.max(IngredientPrice.collected_at),
                func.count(func.distinct(IngredientPrice.vendor_id)),
            ).group_by(IngredientPrice.ingredient_id)
        )
    ).all()
    par_ingredient = {row[0]: row for row in agregats}

    lignes: list[CoverageRow] = []
    prices, obsoletes = 0, 0
    for ingredient in ingredients:
        agregat = par_ingredient.get(ingredient.id)
        if agregat is None:
            etat = PriceFreshness.UNAVAILABLE
            observations, derniere, vendeurs = 0, None, 0
        else:
            _, observations, derniere, vendeurs = agregat
            etat = fraicheur(derniere, maintenant=maintenant)
            prices += 1
            if etat == PriceFreshness.OBSOLETE:
                obsoletes += 1

        if missing_only and etat not in (
            PriceFreshness.UNAVAILABLE,
            PriceFreshness.OBSOLETE,
        ):
            continue

        lignes.append(
            CoverageRow(
                ingredient_slug=ingredient.slug,
                ingredient_name=ingredient.name,
                observations=observations,
                last_collected_at=derniere,
                freshness=etat,
                vendors=vendeurs,
            )
        )

    total = len(ingredients)
    return CoverageOut(
        ingredients_total=total,
        ingredients_priced=prices,
        ingredients_missing=total - prices,
        ingredients_obsolete=obsoletes,
        coverage_ratio=round(prices / total, 3) if total else 0.0,
        generated_at=maintenant,
        rows=lignes,
    )


# --------------------------------------------------------------------------
# Restitution publique (D-09)
# --------------------------------------------------------------------------


@public.get(
    "/ingredients/{slug}/price",
    response_model=PriceRangeOut,
    summary="D-09 — fourchette de prix d'un ingrédient",
)
async def ingredient_price_range(
    slug: str,
    unit: str = Query(description="unité d'observation (kapoaka, kg, botte…)"),
    exclude_official: bool = Query(
        default=False,
        description=(
            "écarter les séries officielles (indices régionaux) pour ne garder "
            "que les relevés effectués chez un commerçant"
        ),
    ),
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PriceRangeOut:
    """Jamais un prix unique : min / médiane / max, avec date et confiance.

    Quand aucune observation exploitable ne subsiste, on renvoie
    `PRICE_UNAVAILABLE` plutôt qu'une fourchette à zéro — qui se lirait comme
    « gratuit ».

    La ventilation `sources` accompagne toujours le chiffre : un prix de
    référence régional et un relevé chez un commerçant ne se valent pas, et
    rien ne doit les faire passer l'un pour l'autre.
    """
    ingredient = await _ingredient_par_slug(session, slug)
    query = select(IngredientPrice).where(
        IngredientPrice.ingredient_id == ingredient.id,
        IngredientPrice.unit == unit,
    )
    if exclude_official:
        query = query.where(IngredientPrice.source != PriceSource.OFFICIAL)
    rows = await session.scalars(query)
    observations = list(rows)
    fourchette = agreger(
        [
            Observation(price=r.price, collected_at=r.collected_at, source=r.source, unit=r.unit)
            for r in observations
        ]
    )
    if fourchette is None:
        raise NutritionError(
            ErrorCode.PRICE_UNAVAILABLE,
            message=f"Aucun prix exploitable pour « {slug} » en {unit}.",
            http_status=status.HTTP_404_NOT_FOUND,
        )

    ventilation: dict[PriceSource, int] = {}
    for row in observations:
        ventilation[row.source] = ventilation.get(row.source, 0) + 1

    return PriceRangeOut(
        ingredient_slug=ingredient.slug,
        unit=unit,
        currency=observations[0].currency,
        price_min=fourchette.minimum,
        price_median=fourchette.mediane,
        price_max=fourchette.maximum,
        observed_at=fourchette.observed_at,
        freshness=fourchette.freshness,
        confidence=fourchette.confidence,
        observations=fourchette.observations,
        usable_for_budget=fourchette.utilisable_pour_budget,
        sources=ventilation,
    )


# --------------------------------------------------------------------------
# FN-017 — coût des plats (sprint 06)
# --------------------------------------------------------------------------


async def _candidats_par_ingredient(
    session: AsyncSession, ingredient_ids: set[uuid.UUID]
) -> dict[uuid.UUID, list[ReleveCandidat]]:
    """Charge en une requête tous les relevés des ingrédients demandés.

    Une requête par ingrédient transformerait le chiffrage d'un panier de dix
    plats en plusieurs centaines d'allers-retours.
    """
    if not ingredient_ids:
        return {}

    lignes = (
        await session.execute(
            select(IngredientPrice, Vendor)
            .join(Vendor, Vendor.id == IngredientPrice.vendor_id)
            .where(IngredientPrice.ingredient_id.in_(ingredient_ids))
        )
    ).all()

    par_ingredient: dict[uuid.UUID, list[ReleveCandidat]] = {}
    for prix, vendeur in lignes:
        par_ingredient.setdefault(prix.ingredient_id, []).append(
            ReleveCandidat(
                vendor_slug=vendeur.slug,
                vendor_zone=vendeur.zone,
                price=prix.price,
                unit=prix.unit,
                quantity=prix.quantity,
                collected_at=prix.collected_at,
                source=prix.source,
            )
        )
    return par_ingredient


def _chiffrer_plat(
    dish: Dish,
    candidats: dict[uuid.UUID, list[ReleveCandidat]],
    *,
    vendeur_prefere: str | None,
    zone: str | None,
    servings: int | None,
    maintenant: datetime,
) -> CoutPlat:
    lignes = [
        cout_ingredient(
            composant.ingredient.slug,
            composant.quantity,
            composant.unit,
            composant.ingredient.reference_unit,
            candidats.get(composant.ingredient_id, []),
            density_g_per_ml=composant.ingredient.density_g_per_ml,
            conversions=[
                IngredientConversion(c.from_unit, c.to_unit, c.factor)
                for c in composant.ingredient.unit_conversions
            ],
            vendeur_prefere=vendeur_prefere,
            zone=zone,
            maintenant=maintenant,
        )
        for composant in dish.ingredients
    ]
    return cout_plat(lignes, servings or dish.servings, maintenant=maintenant)


def _dish_cost_out(dish: Dish, cout: CoutPlat) -> DishCostOut:
    return DishCostOut(
        dish_slug=dish.slug,
        status=str(cout.statut),
        coverage_ratio=cout.couverture,
        currency=DEFAULT_CURRENCY,
        total_cost_min=cout.cout_total_min,
        total_cost_median=cout.cout_total_median,
        total_cost_max=cout.cout_total_max,
        portion_cost_min=cout.cout_portion_min,
        portion_cost_median=cout.cout_portion_median,
        portion_cost_max=cout.cout_portion_max,
        oldest_observation_at=cout.observation_la_plus_ancienne,
        freshness=cout.freshness,
        missing_ingredients=list(cout.ingredients_sans_prix),
        lines=[
            IngredientCostOut(
                ingredient_slug=ligne.ingredient_slug,
                cost_min=ligne.cout_min,
                cost_median=ligne.cout_median,
                cost_max=ligne.cout_max,
                freshness=ligne.freshness,
                retained=(
                    RetainedScopeOut(
                        scope=ligne.retenu.portee,
                        vendor_slug=ligne.retenu.vendor_slug,
                        observed_at=ligne.retenu.observed_at,
                        source=ligne.retenu.source,
                        observations=ligne.retenu.observations,
                    )
                    if ligne.retenu
                    else None
                ),
                reason=ligne.motif,
            )
            for ligne in cout.lignes
        ],
    )


def _dish_query_avec_prix():
    return select(Dish).options(
        selectinload(Dish.ingredients)
        .selectinload(DishIngredient.ingredient)
        .selectinload(Ingredient.unit_conversions)
    )


@public.get(
    "/dishes/{slug}/cost",
    response_model=DishCostOut,
    summary="FN-017 — coût d'un plat, et ce qui n'est pas connu",
)
async def dish_cost(
    slug: str,
    vendor_slug: str | None = Query(default=None, description="chiffrer chez ce vendeur"),
    zone: str | None = Query(default=None, description="repli sur cette zone"),
    servings: int | None = Query(default=None, gt=0, le=50),
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DishCostOut:
    """Le coût n'est jamais donné seul : il vient avec son taux de couverture,
    la date du relevé le plus ancien, et la liste nominative des ingrédients
    non chiffrés."""
    dish = await session.scalar(_dish_query_avec_prix().where(Dish.slug == slug))
    if dish is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"Aucun plat « {slug} ».",
            http_status=status.HTTP_404_NOT_FOUND,
        )

    maintenant = datetime.now(UTC)
    candidats = await _candidats_par_ingredient(
        session, {c.ingredient_id for c in dish.ingredients}
    )
    cout = _chiffrer_plat(
        dish,
        candidats,
        vendeur_prefere=vendor_slug,
        zone=zone,
        servings=servings,
        maintenant=maintenant,
    )
    return _dish_cost_out(dish, cout)


@public.post(
    "/baskets/cost",
    response_model=BasketOut,
    summary="FN-017 — coût d'un panier de plats",
)
async def basket_cost(
    payload: BasketIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BasketOut:
    """Un plat non chiffrable n'est **pas** compté dans le total, et il est
    nommé. Le sommer à zéro ferait passer une absence pour de la gratuité."""
    demandes = list(dict.fromkeys(payload.dish_slugs))
    plats = list(
        await session.scalars(_dish_query_avec_prix().where(Dish.slug.in_(demandes)))
    )
    trouves = {d.slug for d in plats}
    introuvables = [s for s in demandes if s not in trouves]
    if introuvables:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"Plats inconnus : {', '.join(introuvables)}.",
            http_status=status.HTTP_404_NOT_FOUND,
        )

    maintenant = datetime.now(UTC)
    ingredient_ids = {c.ingredient_id for d in plats for c in d.ingredients}
    candidats = await _candidats_par_ingredient(session, ingredient_ids)

    details: list[DishCostOut] = []
    non_chiffres: list[str] = []
    total_min = total_median = total_max = Decimal("0")
    plus_ancienne: datetime | None = None
    lignes_totales = lignes_chiffrees = 0

    for dish in plats:
        cout = _chiffrer_plat(
            dish,
            candidats,
            vendeur_prefere=payload.vendor_slug,
            zone=payload.zone,
            servings=payload.servings,
            maintenant=maintenant,
        )
        details.append(_dish_cost_out(dish, cout))
        lignes_totales += len(cout.lignes)
        lignes_chiffrees += sum(1 for ligne in cout.lignes if ligne.chiffre)

        if cout.cout_total_median is None:
            non_chiffres.append(dish.slug)
            continue

        total_min += cout.cout_total_min or Decimal("0")
        total_median += cout.cout_total_median
        total_max += cout.cout_total_max or Decimal("0")
        if cout.observation_la_plus_ancienne is not None:
            plus_ancienne = (
                cout.observation_la_plus_ancienne
                if plus_ancienne is None
                else min(plus_ancienne, cout.observation_la_plus_ancienne)
            )

    chiffrable = len(non_chiffres) < len(plats)
    couverture = (
        (Decimal(lignes_chiffrees) / Decimal(lignes_totales)).quantize(Decimal("0.001"))
        if lignes_totales
        else Decimal("0")
    )

    return BasketOut(
        currency=DEFAULT_CURRENCY,
        total_cost_min=total_min if chiffrable else None,
        total_cost_median=total_median if chiffrable else None,
        total_cost_max=total_max if chiffrable else None,
        unpriced_dishes=non_chiffres,
        coverage_ratio=couverture,
        freshness=(
            fraicheur(plus_ancienne, maintenant=maintenant)
            if plus_ancienne
            else PriceFreshness.UNAVAILABLE
        ),
        dishes=details,
    )
