"""Collecte de prix — API de la plateforme de pilotage (FN-013, §9.7, plan §5).

Toutes les routes exigent `require_catalog_editor` (règle structurelle de
`tests/test_isolation.py`) ; celles qui déclenchent ou modifient exigent en plus
`require_admin`.

Les trois verrous du plan sont tenus ailleurs, et ce routeur n'en contourne
aucun : il n'importe pas `IngredientPrice`, il ne choisit ni délai ni `robots.txt`
(c'est le moteur), et `bonmarche.mg` n'existe tout simplement pas en base.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_admin, require_catalog_editor
from app.core.config import settings
from app.core.database import get_session
from app.models.audit import AuditAction
from app.models.catalog import Ingredient, IngredientAlias
from app.models.collecte import (
    DELAI_PLANCHER_S,
    OperationRun,
    ScrapedOffer,
    ScrapingError,
    ScrapingSource,
)
from app.models.enums import OperationStatus
from app.routers.pilotage_commun import RoutePilotage, conflit, introuvable, invalide
from app.schemas.pilotage import AliasIn, RunIn, SourcePatch
from app.services import audit, executions
from app.services.collecte import lancement
from app.services.collecte.connecteurs import plateformes
from app.services.collecte.politesse import PLAFOND_PAGES, contact_ascii, meme_site, user_agent
from app.services.collecte.qualification import ECART_MINIMAL
from app.services.pricing import normaliser_libelle

router = APIRouter(
    prefix="/api/v1/admin/scraping",
    tags=["pilotage — collecte"],
    dependencies=[Depends(require_catalog_editor)],
    route_class=RoutePilotage,
)


def _vue_source(source: ScrapingSource, derniere: OperationRun | None) -> dict[str, Any]:
    return {
        "slug": source.slug,
        "name": source.name,
        "platform": source.platform,
        "base_url": source.base_url,
        "pages": list(source.pages or []),
        "delay_seconds": str(source.delay_seconds),
        "max_pages": source.max_pages,
        "is_active": source.is_active,
        "tos_attested_by": source.tos_attested_by,
        "tos_attested_at": source.tos_attested_at.isoformat() if source.tos_attested_at else None,
        "tos_url": source.tos_url,
        "notes": source.notes,
        "derniere_execution": executions.vue_run(derniere) if derniere else None,
    }


async def _source(session: AsyncSession, slug: str) -> ScrapingSource:
    source = await session.scalar(select(ScrapingSource).where(ScrapingSource.slug == slug))
    if source is None:
        raise introuvable(f"Aucune source « {slug} ».")
    return source


async def _derniere(session: AsyncSession, slug: str) -> OperationRun | None:
    return await session.scalar(
        select(OperationRun)
        .where(OperationRun.subject == slug, OperationRun.kind.like("scraping.%"))
        .order_by(OperationRun.created_at.desc())
        .limit(1)
    )


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


@router.get("/sources", summary="Sources de collecte et leur dernière exécution")
async def list_sources(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    sources = list(await session.scalars(select(ScrapingSource).order_by(ScrapingSource.name)))
    return {
        "sources": [_vue_source(s, await _derniere(session, s.slug)) for s in sources],
        "plateformes": plateformes(),
        "delai_plancher_s": DELAI_PLANCHER_S,
        "plafond_pages": PLAFOND_PAGES,
        "agent": user_agent(settings.scraping_contact),
        "contact_renseigne": bool(contact_ascii(settings.scraping_contact)),
    }


@router.patch("/sources/{slug}", summary="Modifier une source (activation, pages, attestation CGU)")
async def update_source(
    slug: str,
    payload: SourcePatch,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await _source(session, slug)
    champs: list[str] = []

    if payload.pages is not None:
        hors_site = [p for p in payload.pages if not meme_site(p, source.base_url)]
        if hors_site:
            raise invalide(f"Pages hors du site {source.base_url} : {', '.join(hors_site[:3])}")
        source.pages = list(dict.fromkeys(payload.pages))
        champs.append("pages")
    if payload.is_active is not None:
        source.is_active = payload.is_active
        champs.append("is_active")
    if payload.delay_seconds is not None:
        source.delay_seconds = payload.delay_seconds
        champs.append("delay_seconds")
    if payload.max_pages is not None:
        source.max_pages = payload.max_pages
        champs.append("max_pages")
    if payload.tos_url is not None:
        source.tos_url = payload.tos_url or None
        champs.append("tos_url")
    if payload.notes is not None:
        source.notes = payload.notes or None
        champs.append("notes")
    if payload.tos_attested is True:
        source.tos_attested_by = principal.external_user_id
        source.tos_attested_at = datetime.now(UTC)
        champs.append("tos_attested")
    elif payload.tos_attested is False:
        source.tos_attested_by = None
        source.tos_attested_at = None
        champs.append("tos_attested")

    if not champs:
        raise invalide("Aucune modification demandée.")

    await audit.record(
        session,
        AuditAction.SCRAPING_SOURCE_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="scraping_source",
        resource_id=source.id,
        details={"source": slug, "champs": champs},
    )
    await session.flush()
    return _vue_source(source, await _derniere(session, slug))


# --------------------------------------------------------------------------
# Exécutions
# --------------------------------------------------------------------------


@router.post("/run", status_code=status.HTTP_202_ACCEPTED, summary="Lancer un relevé ou une collecte à blanc")
async def launch(
    payload: RunIn,
    background: BackgroundTasks,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await _source(session, payload.source)
    if not source.is_active:
        raise conflit(f"La source « {source.slug} » est désactivée.")
    if await executions.en_cours_pour(session, source.slug):
        raise conflit(f"Une exécution est déjà en cours sur « {source.slug} ».")
    if payload.pages:
        inconnues = [p for p in payload.pages if p not in (source.pages or [])]
        if inconnues:
            raise invalide("Pages absentes de la configuration de la source : " + ", ".join(inconnues[:3]))

    avertissement = None
    if payload.mode == "structure":
        precedent = await session.scalar(
            select(func.max(OperationRun.started_at)).where(
                OperationRun.subject == source.slug,
                OperationRun.kind == executions.KIND_STRUCTURE,
                OperationRun.status == OperationStatus.SUCCEEDED,
            )
        )
        if precedent and datetime.now(UTC) - precedent < ECART_MINIMAL:
            avertissement = (
                f"Un relevé de structure a déjà été fait le {precedent:%d/%m/%Y} : "
                "celui-ci ne comptera pas pour le critère 3 (relevés espacés d'une semaine)."
            )

    run = await executions.creer(
        session,
        kind=executions.KIND_STRUCTURE if payload.mode == "structure" else executions.KIND_COLLECTE,
        subject=source.slug,
        params={"mode": payload.mode, "pages": payload.pages or []},
        requested_by=principal.external_user_id,
    )
    await audit.record(
        session,
        AuditAction.COLLECTION_STARTED,
        external_user_id=principal.external_user_id,
        resource_type="operation_run",
        resource_id=run.id,
        details={"source": source.slug, "mode": payload.mode},
    )
    # Validé avant la tâche de fond : elle ouvre sa propre session et doit voir la ligne.
    await session.commit()
    background.add_task(lancement.executer_en_arriere_plan, run.id)
    return {
        "run_id": str(run.id),
        "status": OperationStatus.PENDING.value,
        "poll_after_ms": 1500,
        "avertissement": avertissement,
    }


@router.get("/runs", summary="Historique des exécutions")
async def list_runs(
    source: str | None = Query(default=None, max_length=64),
    limite: int = Query(default=30, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    requete = select(OperationRun).where(OperationRun.kind.like("scraping.%"))
    if source:
        requete = requete.where(OperationRun.subject == source)
    runs = await session.scalars(requete.order_by(OperationRun.created_at.desc()).limit(limite))
    return {"runs": [executions.vue_run(r) for r in runs]}


@router.get("/runs/{run_id}", summary="Progression et événements depuis `apres`")
async def run_detail(
    run_id: uuid.UUID,
    apres: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(OperationRun, run_id)
    if run is None or not run.kind.startswith("scraping."):
        raise introuvable("Exécution introuvable.")
    evenements = await executions.evenements_depuis(session, run_id, apres)
    return {
        "run": executions.vue_run(run),
        "events": [executions.vue_evenement(e) for e in evenements],
        "instantanes": (executions.dossier_instantanes(run_id) / "pages.json").exists(),
    }


@router.post("/runs/{run_id}/cancel", summary="Demander l'arrêt avant la page suivante")
async def cancel(
    run_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(OperationRun, run_id)
    if run is None or not run.kind.startswith("scraping."):
        raise introuvable("Exécution introuvable.")
    if run.status not in executions.EN_COURS:
        raise conflit("Cette exécution est déjà terminée.")
    run.cancel_requested = True
    await audit.record(
        session,
        AuditAction.COLLECTION_CANCELLED,
        external_user_id=principal.external_user_id,
        resource_type="operation_run",
        resource_id=run.id,
        details={"source": run.subject},
    )
    await session.flush()
    return {"run": executions.vue_run(run)}


@router.post(
    "/runs/{run_id}/replay",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ré-extraire sur les instantanés, sans requête au site",
)
async def replay(
    run_id: uuid.UUID,
    background: BackgroundTasks,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    origine = await session.get(OperationRun, run_id)
    if origine is None or not origine.kind.startswith("scraping."):
        raise introuvable("Exécution introuvable.")
    if not (executions.dossier_instantanes(run_id) / "pages.json").exists():
        raise conflit("Cette exécution n'a conservé aucune page : rien à rejouer.")
    if await executions.en_cours_pour(session, origine.subject or ""):
        raise conflit(f"Une exécution est déjà en cours sur « {origine.subject} ».")

    run = await executions.creer(
        session,
        kind=executions.KIND_REJEU,
        subject=origine.subject,
        params={"mode": "rejeu", "run_source": str(origine.id)},
        requested_by=principal.external_user_id,
    )
    await session.commit()
    background.add_task(lancement.rejouer_en_arriere_plan, run.id)
    return {"run_id": str(run.id), "status": OperationStatus.PENDING.value, "poll_after_ms": 1000}


@router.get("/runs/{run_id}/offers", summary="Offres extraites (en transit, jamais des prix observés)")
async def offers(
    run_id: uuid.UUID,
    filtre: Literal["toutes", "appariees", "a_revoir", "prix_illisibles"] = Query(default="toutes"),
    limite: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    requete = (
        select(ScrapedOffer, Ingredient.slug, Ingredient.name)
        .outerjoin(Ingredient, Ingredient.id == ScrapedOffer.ingredient_id)
        .where(ScrapedOffer.run_id == run_id)
    )
    if filtre == "appariees":
        requete = requete.where(ScrapedOffer.match_status == "matched")
    elif filtre == "a_revoir":
        requete = requete.where(ScrapedOffer.match_status == "unmatched")
    elif filtre == "prix_illisibles":
        requete = requete.where(ScrapedOffer.price_status == "unreadable")

    lignes = (await session.execute(requete.order_by(ScrapedOffer.label_raw).limit(limite))).all()
    return {
        "offres": [
            {
                "id": str(o.id),
                "url": o.url,
                "libelle": o.label_raw,
                "prix_texte": o.price_raw,
                "prix": float(o.price) if o.price is not None else None,
                "devise": o.currency,
                "statut_prix": o.price_status,
                "motif_prix": o.price_reason,
                "conditionnement": o.packaging_raw,
                "quantite": float(o.quantity) if o.quantity is not None else None,
                "unite": o.unit,
                "disponibilite": o.availability,
                "ingredient": slug,
                "ingredient_nom": nom,
                "statut_appariement": o.match_status,
                "motif_appariement": o.match_reason,
            }
            for o, slug, nom in lignes
        ],
        "total": len(lignes),
    }


@router.get("/errors", summary="Erreurs de collecte, avec leur contexte")
async def errors(
    run_id: uuid.UUID | None = Query(default=None),
    limite: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    requete = select(ScrapingError, ScrapingSource.slug).outerjoin(
        ScrapingSource, ScrapingSource.id == ScrapingError.source_id
    )
    if run_id:
        requete = requete.where(ScrapingError.run_id == run_id)
    lignes = (
        await session.execute(requete.order_by(ScrapingError.occurred_at.desc()).limit(limite))
    ).all()
    return {
        "erreurs": [
            {
                "run_id": str(e.run_id),
                "source": slug,
                "url": e.url,
                "etape": e.step,
                "http_status": e.http_status,
                "message": e.message,
                "contexte": e.context or {},
                "at": e.occurred_at.isoformat(),
            }
            for e, slug in lignes
        ]
    }


# --------------------------------------------------------------------------
# File de revue
# --------------------------------------------------------------------------


@router.get("/review", summary="Libellés non appariés, regroupés")
async def review(
    limite: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    lignes = (
        await session.execute(
            select(
                ScrapedOffer.label_normalized,
                func.min(ScrapedOffer.label_raw),
                func.count(),
                func.max(ScrapedOffer.collected_at),
                func.min(ScrapedOffer.price_raw),
                func.array_agg(func.distinct(ScrapingSource.slug)),
            )
            .outerjoin(ScrapingSource, ScrapingSource.id == ScrapedOffer.source_id)
            .where(ScrapedOffer.match_status == "unmatched")
            .group_by(ScrapedOffer.label_normalized)
            .order_by(func.count().desc(), ScrapedOffer.label_normalized)
            .limit(limite)
        )
    ).all()
    ingredients = (
        await session.execute(select(Ingredient.slug, Ingredient.name).order_by(Ingredient.name))
    ).all()
    return {
        "libelles": [
            {
                "libelle_normalise": normalise,
                "exemple": exemple,
                "occurrences": n,
                "derniere_vue": derniere.isoformat() if derniere else None,
                "exemple_prix": prix,
                "sources": sorted(s for s in (sources or []) if s),
            }
            for normalise, exemple, n, derniere, prix, sources in lignes
        ],
        "ingredients": [{"slug": s, "name": n} for s, n in ingredients],
    }


@router.post("/review/aliases", summary="Associer un libellé à un ingrédient (appariement exact)")
async def add_alias(
    payload: AliasIn,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ingredient = await session.scalar(select(Ingredient).where(Ingredient.slug == payload.ingredient))
    if ingredient is None:
        raise introuvable(f"Aucun ingrédient « {payload.ingredient} ».")

    normalise = normaliser_libelle(payload.label)
    index, _ = await lancement.construire_index(session)
    deja = index.get(normalise)
    if deja and deja != ingredient.slug:
        raise conflit(f"Ce libellé est déjà apparié à « {deja} » : l'appariement doit rester univoque.")

    if not deja:
        session.add(
            IngredientAlias(
                id=uuid.uuid4(),
                ingredient_id=ingredient.id,
                alias=payload.label[:200],
                language=payload.language,
            )
        )

    resultat = await session.execute(
        update(ScrapedOffer)
        .where(ScrapedOffer.label_normalized == normalise, ScrapedOffer.match_status == "unmatched")
        .values(
            ingredient_id=ingredient.id,
            match_status="matched",
            match_reason="alias ajouté depuis la file de revue",
        )
    )
    await audit.record(
        session,
        AuditAction.INGREDIENT_ALIAS_ADDED,
        external_user_id=principal.external_user_id,
        resource_type="ingredient",
        resource_id=ingredient.id,
        details={"ingredient": ingredient.slug, "offres_reappariees": resultat.rowcount or 0},
    )
    await session.flush()
    return {
        "ingredient": ingredient.slug,
        "alias": payload.label,
        "alias_cree": not deja,
        "offres_reappariees": resultat.rowcount or 0,
    }


# --------------------------------------------------------------------------
# SPIKE-01
# --------------------------------------------------------------------------


@router.get("/qualification", summary="SPIKE-01 — les quatre critères, mesurés")
async def qualification(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    etat = await lancement.etat_qualification(session)
    resultat = etat.as_dict()
    for source in resultat["sources"]:
        prochain = source["prochain_releve"]
        source["prochain_releve"] = prochain.isoformat() if prochain else None
        source["releve_du"] = (
            prochain is not None and prochain <= datetime.now(UTC) + timedelta(hours=12)
        )
    return resultat


@router.post("/import-spike01", summary="Importer spike01/releves.jsonl (sans doublon)")
async def import_archive(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    resultat = await lancement.importer_archive_spike01(session)
    await session.commit()
    return resultat
