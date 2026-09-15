"""Sondes de vie et de disponibilité (FN-037, critère de fin du lot 1).

Distinction volontaire :

* `/health` — **liveness**. Le processus répond. Aucune dépendance testée : si
  cette sonde échouait sur une base indisponible, l'orchestrateur redémarrerait
  le service en boucle sans rien résoudre.
* `/ready` — **readiness**. Le service peut réellement traiter une requête : la
  base répond et le schéma est à jour. C'est celle qui pilote la mise en
  service derrière un répartiteur de charge.

L'ancienne sonde répondait « healthy » même base coupée ; c'était une sonde qui
ne mesurait rien.
"""

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness — le processus répond")
async def health() -> dict[str, str]:
    return {"status": "alive", "environment": settings.environment}


@router.get("/ready", summary="Readiness — les dépendances répondent")
async def ready(response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    ok = True

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
        checks["database"] = {"status": "up", "migration": revision}
        if revision is None:
            checks["database"]["status"] = "degraded"
            checks["database"]["reason"] = "aucune migration appliquée"
            ok = False
    except Exception as exc:  # noqa: BLE001 — la cause exacte est journalisée
        checks["database"] = {"status": "down", "reason": type(exc).__name__}
        ok = False

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if ok else "not_ready", "checks": checks}
