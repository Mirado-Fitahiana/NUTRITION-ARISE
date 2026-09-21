"""Exécutions longues de la plateforme de pilotage (plan §4.3).

Le mécanisme est celui de la génération (`planning.py`) : `202 Accepted`, tâche
de fond native avec **sa propre session**, sondage par le client. Pas de Celery
ni de Redis — FN-017 et le sprint 09 l'écartent à ce volume.

Deux points qui ne se voient qu'en exploitation :

* **transactions courtes.** Une collecte attend plusieurs secondes entre deux
  pages ; garder une transaction ouverte pendant ce temps bloquerait des
  verrous pour rien. Chaque écriture ouvre et ferme sa session ;
* **reprise au démarrage.** En développement, `run.py` redémarre uvicorn à
  chaque sauvegarde. Une exécution encore `running` au démarrage a perdu son
  processus : elle passe en `interrupted`, sinon elle resterait « en cours »
  indéfiniment à l'écran. Cette reprise suppose **un seul processus** serveur.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import SessionFactory
from app.core.logging import get_correlation_id
from app.models.collecte import OperationRun, OperationRunEvent, ScrapedOffer, ScrapingError
from app.models.enums import OperationStatus

logger = logging.getLogger("app.executions")

KIND_STRUCTURE = "scraping.structure"
KIND_COLLECTE = "scraping.collecte"
KIND_REJEU = "scraping.rejeu"

EN_COURS = (OperationStatus.PENDING, OperationStatus.RUNNING)

ROOT = Path(__file__).resolve().parents[2]


def maintenant() -> datetime:
    return datetime.now(UTC)


def dossier_instantanes(run_id: uuid.UUID) -> Path:
    base = Path(settings.scraping_snapshot_dir)
    if not base.is_absolute():
        base = ROOT / base
    return base / str(run_id)


# --------------------------------------------------------------------------
# Cycle de vie
# --------------------------------------------------------------------------


async def creer(
    session: AsyncSession,
    *,
    kind: str,
    subject: str | None,
    params: dict[str, Any],
    requested_by: str | None,
) -> OperationRun:
    run = OperationRun(
        id=uuid.uuid4(),
        kind=kind,
        subject=subject,
        status=OperationStatus.PENDING,
        params=params,
        counters={},
        done=0,
        alert=False,
        cancel_requested=False,
        requested_by=requested_by,
        correlation_id=get_correlation_id() if get_correlation_id() != "-" else None,
        created_at=maintenant(),
    )
    session.add(run)
    await session.flush()
    return run


async def en_cours_pour(session: AsyncSession, subject: str) -> OperationRun | None:
    return await session.scalar(
        select(OperationRun)
        .where(OperationRun.subject == subject, OperationRun.status.in_(EN_COURS))
        .limit(1)
    )


async def interrompre_orphelins() -> int:
    """Marque `interrupted` les exécutions dont le processus a disparu."""
    async with SessionFactory() as session:
        resultat = await session.execute(
            update(OperationRun)
            .where(OperationRun.status.in_(EN_COURS))
            .values(
                status=OperationStatus.INTERRUPTED,
                finished_at=maintenant(),
                error_code="INTERRUPTED",
                error_detail=(
                    "Le serveur a redémarré pendant l'exécution (rechargement "
                    "automatique en développement). Relancer si nécessaire."
                ),
            )
        )
        await session.commit()
        return resultat.rowcount or 0


async def marquer_demarree(run_id: uuid.UUID) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(OperationRun)
            .where(OperationRun.id == run_id)
            .values(status=OperationStatus.RUNNING, started_at=maintenant())
        )
        await session.commit()


async def cloturer(
    run_id: uuid.UUID,
    *,
    statut: OperationStatus,
    compteurs: dict[str, Any] | None = None,
    alerte: bool = False,
    code: str | None = None,
    detail: str | None = None,
) -> None:
    valeurs: dict[str, Any] = {
        "status": statut,
        "finished_at": maintenant(),
        "alert": alerte,
        "error_code": code,
        "error_detail": (detail or None) and detail[:2000],
    }
    if compteurs is not None:
        valeurs["counters"] = _json(compteurs)
    async with SessionFactory() as session:
        await session.execute(update(OperationRun).where(OperationRun.id == run_id).values(**valeurs))
        await session.commit()


async def evenements_depuis(
    session: AsyncSession, run_id: uuid.UUID, apres: int, limite: int = 300
) -> list[OperationRunEvent]:
    return list(
        await session.scalars(
            select(OperationRunEvent)
            .where(OperationRunEvent.run_id == run_id, OperationRunEvent.seq > apres)
            .order_by(OperationRunEvent.seq)
            .limit(limite)
        )
    )


# --------------------------------------------------------------------------
# Sérialisation
# --------------------------------------------------------------------------


def _json(valeur: Any) -> Any:
    """JSONB n'accepte ni `Decimal` ni `datetime` : on convertit à l'écriture."""
    return json.loads(json.dumps(valeur, default=str))


def vue_run(run: OperationRun) -> dict[str, Any]:
    fin = run.finished_at or (maintenant() if run.started_at else None)
    return {
        "id": str(run.id),
        "kind": run.kind,
        "subject": run.subject,
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "params": run.params or {},
        "step": run.step,
        "done": run.done,
        "total": run.total,
        "counters": run.counters or {},
        "alert": run.alert,
        "requested_by": run.requested_by,
        "correlation_id": run.correlation_id,
        "cancel_requested": run.cancel_requested,
        "error_code": run.error_code,
        "error_detail": run.error_detail,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": (
            int((fin - run.started_at).total_seconds() * 1000)
            if run.started_at and fin
            else None
        ),
    }


def vue_evenement(evenement: OperationRunEvent) -> dict[str, Any]:
    return {
        "seq": evenement.seq,
        "at": evenement.occurred_at.isoformat(),
        "level": evenement.level,
        "step": evenement.step,
        "message": evenement.message,
        "data": evenement.data or {},
    }


# --------------------------------------------------------------------------
# Rapporteur SQL
# --------------------------------------------------------------------------


class RapporteurSql:
    """Implémentation persistante du contrat `moteur.Rapporteur`.

    Une seule tâche écrit dans une exécution donnée : le numéro d'ordre des
    événements est tenu en mémoire, sans verrou.
    """

    def __init__(
        self,
        run_id: uuid.UUID,
        *,
        source_id: uuid.UUID | None = None,
        ids_ingredients: dict[str, uuid.UUID] | None = None,
        conserver_instantanes: bool = True,
    ) -> None:
        self.run_id = run_id
        self.source_id = source_id
        self.ids_ingredients = ids_ingredients or {}
        self.conserver_instantanes = conserver_instantanes
        self._seq = 0
        self._pages: list[dict[str, Any]] = []

    async def evenement(self, niveau, etape, message, donnees=None) -> None:
        self._seq += 1
        async with SessionFactory() as session:
            session.add(
                OperationRunEvent(
                    id=uuid.uuid4(),
                    run_id=self.run_id,
                    seq=self._seq,
                    occurred_at=maintenant(),
                    level=niveau,
                    step=etape,
                    message=str(message)[:2000],
                    data=_json(donnees or {}),
                )
            )
            await session.commit()

    async def progression(self, etape, fait, total) -> None:
        async with SessionFactory() as session:
            await session.execute(
                update(OperationRun)
                .where(OperationRun.id == self.run_id)
                .values(step=etape, done=max(0, int(fait)), total=total)
            )
            await session.commit()

    async def compteurs(self, compteurs) -> None:
        async with SessionFactory() as session:
            await session.execute(
                update(OperationRun)
                .where(OperationRun.id == self.run_id)
                .values(counters=_json(compteurs))
            )
            await session.commit()

    async def offres(self, offres) -> None:
        horodatage = maintenant()
        async with SessionFactory() as session:
            for offre in offres:
                session.add(
                    ScrapedOffer(
                        id=uuid.uuid4(),
                        run_id=self.run_id,
                        source_id=self.source_id,
                        url=(offre.url or None) and offre.url[:1000],
                        label_raw=offre.libelle,
                        label_normalized=offre.libelle_normalise,
                        price_raw=offre.prix_texte,
                        price=offre.prix,
                        currency=offre.devise,
                        price_status=offre.statut_prix,
                        price_reason=(offre.motif_prix or None) and offre.motif_prix[:255],
                        packaging_raw=(offre.conditionnement or None) and offre.conditionnement[:200],
                        quantity=offre.quantite,
                        unit=offre.unite,
                        availability=(offre.disponibilite or None) and offre.disponibilite[:40],
                        ingredient_id=self.ids_ingredients.get(offre.ingredient_slug or ""),
                        match_status=offre.statut_appariement,
                        match_reason=offre.motif_appariement[:255],
                        collected_at=horodatage,
                    )
                )
            await session.commit()

    async def erreur(self, etape, message, *, url=None, http_status=None, contexte=None) -> None:
        async with SessionFactory() as session:
            session.add(
                ScrapingError(
                    id=uuid.uuid4(),
                    run_id=self.run_id,
                    source_id=self.source_id,
                    url=(url or None) and url[:1000],
                    step=etape,
                    http_status=http_status,
                    message=str(message)[:2000],
                    context=_json(contexte or {}),
                    occurred_at=maintenant(),
                )
            )
            await session.commit()
        await self.evenement("error", etape, message, {"url": url, "http_status": http_status})

    async def instantane(self, index, url, html) -> None:
        if not self.conserver_instantanes:
            return
        dossier = dossier_instantanes(self.run_id)
        self._pages.append({"index": index, "url": url, "fichier": f"{index:03d}.html"})

        def _ecrire() -> None:
            dossier.mkdir(parents=True, exist_ok=True)
            (dossier / f"{index:03d}.html").write_text(html, encoding="utf-8")
            (dossier / "pages.json").write_text(
                json.dumps(self._pages, ensure_ascii=False, indent=1), encoding="utf-8"
            )

        try:
            await asyncio.to_thread(_ecrire)
        except OSError as exc:
            # Un instantané manquant empêche le rejeu, pas la collecte.
            logger.warning("instantané non écrit pour %s : %s", self.run_id, exc)

    async def annulation_demandee(self) -> bool:
        async with SessionFactory() as session:
            return bool(
                await session.scalar(
                    select(OperationRun.cancel_requested).where(OperationRun.id == self.run_id)
                )
            )


def charger_instantanes(run_id: uuid.UUID) -> list[tuple[str, str]]:
    """Pages conservées d'une exécution, dans l'ordre. Vide si rien n'a été gardé."""
    dossier = dossier_instantanes(run_id)
    index = dossier / "pages.json"
    if not index.exists():
        return []
    pages = json.loads(index.read_text(encoding="utf-8"))
    resultat: list[tuple[str, str]] = []
    for page in sorted(pages, key=lambda p: p["index"]):
        fichier = dossier / page["fichier"]
        if fichier.exists():
            resultat.append((page["url"], fichier.read_text(encoding="utf-8")))
    return resultat


__all__ = [
    "EN_COURS",
    "KIND_COLLECTE",
    "KIND_REJEU",
    "KIND_STRUCTURE",
    "RapporteurSql",
    "charger_instantanes",
    "cloturer",
    "creer",
    "dossier_instantanes",
    "en_cours_pour",
    "evenements_depuis",
    "interrompre_orphelins",
    "marquer_demarree",
    "vue_evenement",
    "vue_run",
]
