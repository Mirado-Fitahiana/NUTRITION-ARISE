"""Pont entre le moteur de collecte, la base et les tâches de fond.

Le moteur (`moteur.py`) ne connaît ni SQLAlchemy ni la configuration ; ce module
lui prépare ce qu'il lit — la source, l'index d'appariement, les réglages
effectifs — puis confie la persistance au `RapporteurSql`.

Il porte aussi deux lectures dont la page de collecte a besoin : la
qualification SPIKE-01 mesurée, et l'import de l'archive du script
`spike01_releve.py`, pour que le comptage hebdomadaire ne reparte pas de zéro.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import SessionFactory
from app.models.audit import AuditAction
from app.models.catalog import Ingredient
from app.models.collecte import OperationRun, ScrapingSource
from app.models.enums import AuditResult, OperationStatus
from app.services import audit, executions, reglages
from app.services.collecte import moteur
from app.services.collecte.politesse import contact_ascii
from app.services.collecte.qualification import (
    EtatSource,
    Qualification,
    Releve,
    lire_archive_spike01,
    qualifier,
)
from app.services.pricing import normaliser_libelle

logger = logging.getLogger("app.collecte")

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE_SPIKE01 = ROOT / "spike01" / "releves.jsonl"


async def construire_index(
    session: AsyncSession,
) -> tuple[dict[str, str], dict[str, uuid.UUID]]:
    """Libellés normalisés → slug, et slug → id. Même index que
    `scripts/import_prix.py` : slugs, noms et alias, appariement exact."""
    index: dict[str, str] = {}
    ids: dict[str, uuid.UUID] = {}
    ingredients = await session.scalars(
        select(Ingredient).options(selectinload(Ingredient.aliases))
    )
    for ingredient in ingredients:
        ids[ingredient.slug] = ingredient.id
        index[normaliser_libelle(ingredient.slug)] = ingredient.slug
        index[normaliser_libelle(ingredient.name)] = ingredient.slug
        for alias in ingredient.aliases:
            index.setdefault(normaliser_libelle(alias.alias), ingredient.slug)
    return index, ids


def source_collecte(row: ScrapingSource) -> moteur.SourceCollecte:
    return moteur.SourceCollecte(
        slug=row.slug,
        nom=row.name,
        plateforme=row.platform,
        base_url=row.base_url,
        pages=tuple(row.pages or ()),
        delai_s=Decimal(str(row.delay_seconds)),
        max_pages=row.max_pages,
    )


async def _preparer(run_id: uuid.UUID) -> tuple[OperationRun, ScrapingSource, dict, dict, dict] | None:
    async with SessionFactory() as session:
        run = await session.get(OperationRun, run_id)
        if run is None:
            return None
        source = await session.scalar(
            select(ScrapingSource).where(ScrapingSource.slug == run.subject)
        )
        if source is None:
            await executions.cloturer(
                run_id,
                statut=OperationStatus.FAILED,
                code="SOURCE_INTROUVABLE",
                detail=f"Aucune source « {run.subject} ».",
            )
            return None
        index, ids = await construire_index(session)
        valeurs = reglages.valeurs_collecte(await reglages.lire_etats(session))
        return run, source, index, ids, valeurs


async def _journaliser_echec(run_id: uuid.UUID, subject: str | None, bilan: moteur.Bilan) -> None:
    try:
        async with SessionFactory() as session:
            await audit.record(
                session,
                AuditAction.COLLECTION_FAILED,
                resource_type="operation_run",
                resource_id=run_id,
                result=AuditResult.FAILURE,
                details={
                    "source": subject,
                    "code": bilan.code_erreur,
                    "alerte_structure": bilan.alerte,
                },
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — un journal indisponible ne masque pas le bilan
        logger.exception("journal d'audit indisponible pour l'exécution %s", run_id)


async def executer_en_arriere_plan(run_id: uuid.UUID) -> None:
    """Tâche de fond d'une collecte ou d'un relevé de structure."""
    try:
        prepare = await _preparer(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("préparation impossible pour %s", run_id)
        await executions.cloturer(
            run_id, statut=OperationStatus.FAILED, code="PREPARATION", detail=type(exc).__name__
        )
        return
    if prepare is None:
        return
    run, source_row, index, ids, valeurs = prepare

    await executions.marquer_demarree(run_id)
    rapporteur = executions.RapporteurSql(run_id, source_id=source_row.id, ids_ingredients=ids)
    params_run: dict[str, Any] = dict(run.params or {})
    parametres = moteur.ParametresCollecte(
        mode=str(params_run.get("mode") or moteur.MODE_STRUCTURE),
        contact=settings.scraping_contact,
        delai_reglage_s=valeurs["scraping_min_delay_s"],
        max_pages_reglage=valeurs["scraping_max_pages"],
        seuil_alerte=valeurs["scraping_failure_alert_ratio"],
        pages=tuple(params_run.get("pages") or ()) or None,
    )

    try:
        async with moteur.creer_client(settings.scraping_contact) as client:
            bilan = await moteur.executer(
                source_collecte(source_row),
                parametres,
                client=client,
                rapporteur=rapporteur,
                index=index,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("collecte %s interrompue par une erreur", run_id)
        bilan = moteur.Bilan("failed", {}, False, "INTERNAL_ERROR", type(exc).__name__)
        try:
            await rapporteur.evenement("error", None, f"Erreur interne : {type(exc).__name__}")
        except Exception:  # noqa: BLE001
            pass

    await executions.cloturer(
        run_id,
        statut=OperationStatus(bilan.statut),
        compteurs=bilan.compteurs or None,
        alerte=bilan.alerte,
        code=bilan.code_erreur,
        detail=bilan.detail_erreur,
    )
    if bilan.statut == "failed" or bilan.alerte:
        await _journaliser_echec(run_id, run.subject, bilan)


async def rejouer_en_arriere_plan(run_id: uuid.UUID) -> None:
    """Ré-extraction sur les instantanés d'une exécution précédente."""
    try:
        prepare = await _preparer(run_id)
    except Exception as exc:  # noqa: BLE001
        await executions.cloturer(
            run_id, statut=OperationStatus.FAILED, code="PREPARATION", detail=type(exc).__name__
        )
        return
    if prepare is None:
        return
    run, source_row, index, ids, valeurs = prepare

    origine = uuid.UUID(str(run.params.get("run_source")))
    instantanes = executions.charger_instantanes(origine)
    if not instantanes:
        await executions.cloturer(
            run_id,
            statut=OperationStatus.FAILED,
            code="AUCUN_INSTANTANE",
            detail="L'exécution d'origine n'a conservé aucune page.",
        )
        return

    await executions.marquer_demarree(run_id)
    rapporteur = executions.RapporteurSql(
        run_id, source_id=source_row.id, ids_ingredients=ids, conserver_instantanes=False
    )
    try:
        bilan = await moteur.rejouer(
            source_collecte(source_row),
            instantanes,
            moteur.ParametresCollecte(
                mode=moteur.MODE_COLLECTE, seuil_alerte=valeurs["scraping_failure_alert_ratio"]
            ),
            rapporteur=rapporteur,
            index=index,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rejeu %s interrompu", run_id)
        bilan = moteur.Bilan("failed", {}, False, "INTERNAL_ERROR", type(exc).__name__)

    await executions.cloturer(
        run_id,
        statut=OperationStatus(bilan.statut),
        compteurs=bilan.compteurs or None,
        alerte=bilan.alerte,
        code=bilan.code_erreur,
        detail=bilan.detail_erreur,
    )


# --------------------------------------------------------------------------
# Qualification et archive SPIKE-01
# --------------------------------------------------------------------------


def releve_depuis_run(run: OperationRun) -> Releve:
    compteurs = run.counters or {}
    statut = run.status.value if hasattr(run.status, "value") else str(run.status)
    return Releve(
        horodatage=run.started_at or run.created_at,
        mode="structure" if run.kind == executions.KIND_STRUCTURE else "collecte",
        reussi=statut == OperationStatus.SUCCEEDED.value,
        produits=int(compteurs.get("produits") or 0),
        prix_trouves=int(compteurs.get("prix_trouves") or 0),
        exemples_prix=tuple(compteurs.get("exemples_prix") or ()),
        robots_autorise=compteurs.get("robots_autorise"),
        erreur=run.error_detail if statut != OperationStatus.SUCCEEDED.value else None,
        ingredients_apparies=compteurs.get("ingredients_apparies"),
    )


async def etat_qualification(session: AsyncSession) -> Qualification:
    sources = list(await session.scalars(select(ScrapingSource).order_by(ScrapingSource.name)))
    runs = list(
        await session.scalars(
            select(OperationRun)
            .where(
                OperationRun.kind.in_([executions.KIND_STRUCTURE, executions.KIND_COLLECTE])
            )
            .order_by(OperationRun.created_at)
        )
    )
    referentiel = await session.scalar(select(func.count()).select_from(Ingredient))

    par_source: dict[str, list[Releve]] = {}
    for run in runs:
        if run.status in (OperationStatus.PENDING, OperationStatus.RUNNING):
            continue
        par_source.setdefault(run.subject or "", []).append(releve_depuis_run(run))

    return qualifier(
        [
            EtatSource(
                slug=s.slug,
                nom=s.name,
                actif=s.is_active,
                releves=tuple(par_source.get(s.slug, ())),
                cgu_attestees_par=s.tos_attested_by,
                cgu_attestees_le=s.tos_attested_at,
                cgu_lien=s.tos_url,
            )
            for s in sources
        ],
        referentiel_ingredients=referentiel,
        contact_renseigne=bool(contact_ascii(settings.scraping_contact)),
    )


async def importer_archive_spike01(
    session: AsyncSession, chemin: Path = ARCHIVE_SPIKE01
) -> dict[str, Any]:
    """Importe les relevés du script, sans doublon : un relevé déjà présent
    (même source, même horodatage) est ignoré. Rejouable."""
    if not chemin.exists():
        return {"importes": 0, "deja_presents": 0, "sources_inconnues": [], "lignes": 0}

    lignes = lire_archive_spike01(chemin.read_text(encoding="utf-8"))
    slugs = set(await session.scalars(select(ScrapingSource.slug)))
    importes = deja = 0
    inconnues: set[str] = set()

    for ligne in lignes:
        if ligne["source"] not in slugs:
            inconnues.add(ligne["source"])
            continue
        horodatage: datetime = ligne["horodatage"]
        if horodatage.tzinfo is None:
            horodatage = horodatage.replace(tzinfo=UTC)
        existe = await session.scalar(
            select(func.count())
            .select_from(OperationRun)
            .where(
                OperationRun.kind == executions.KIND_STRUCTURE,
                OperationRun.subject == ligne["source"],
                OperationRun.started_at == horodatage,
            )
        )
        if existe:
            deja += 1
            continue
        reussi = ligne["erreur"] is None
        session.add(
            OperationRun(
                id=uuid.uuid4(),
                kind=executions.KIND_STRUCTURE,
                subject=ligne["source"],
                status=OperationStatus.SUCCEEDED if reussi else OperationStatus.FAILED,
                params={"mode": "structure", "origine": "spike01_releve.py"},
                step="rapport",
                done=1,
                total=1,
                counters={
                    "mode": "structure",
                    "pages_total": 1,
                    "pages_lues": 1 if reussi else 0,
                    "http_status": ligne["http_status"],
                    "robots_autorise": ligne["robots_autorise"],
                    "produits": ligne["produits"],
                    "prix_trouves": ligne["prix_trouves"],
                    "exemples_prix": ligne["exemples_prix"],
                    "taille_octets": ligne["taille_octets"],
                },
                alert=False,
                requested_by="spike01_releve.py",
                cancel_requested=False,
                error_code=None if reussi else "RELEVE_EN_ERREUR",
                error_detail=ligne["erreur"],
                created_at=horodatage,
                started_at=horodatage,
                finished_at=horodatage,
            )
        )
        importes += 1

    await session.flush()
    return {
        "importes": importes,
        "deja_presents": deja,
        "sources_inconnues": sorted(inconnues),
        "lignes": len(lignes),
    }


__all__ = [
    "ARCHIVE_SPIKE01",
    "construire_index",
    "etat_qualification",
    "executer_en_arriere_plan",
    "importer_archive_spike01",
    "rejouer_en_arriere_plan",
    "releve_depuis_run",
    "source_collecte",
]
