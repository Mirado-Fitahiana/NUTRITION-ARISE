"""Point d'entrée du service Nutrition ARISE.

Service autonome (§4.2) : base dédiée, migrations propres, aucun appel
synchrone vers NestJS pendant un traitement nutritionnel.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.core.config import settings
from app.core.database import engine
from app.core.errors import register_exception_handlers
from app.core.logging import (
    CORRELATION_HEADER,
    configure_logging,
    set_correlation_id,
)
from app.routers import (
    admin_collecte,
    admin_laboratoire,
    admin_reglages,
    admin_supervision,
    ai_test,
    catalog,
    console,
    health,
    pilotage,
    planning,
    pricing,
    profile,
)
from app.services import executions

logger = logging.getLogger("app.main")


async def _reprendre_executions() -> None:
    """Une exécution encore « en cours » au démarrage a perdu son processus
    (rechargement d'uvicorn en développement) : elle passe en `interrupted`.
    Base injoignable ou migration absente : on démarre quand même."""
    try:
        nombre = await asyncio.wait_for(executions.interrompre_orphelins(), timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.info("reprise des exécutions non effectuée (%s)", type(exc).__name__)
        return
    if nombre:
        logger.warning("%s exécution(s) marquée(s) interrompue(s) au démarrage", nombre)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    await _reprendre_executions()
    yield
    await engine.dispose()


app = FastAPI(
    title="ARISE Nutrition Service",
    description=(
        "Profil nutritionnel, catalogue de plats, génération de programmes "
        "alimentaires et chiffrage des achats."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """FN-036 — accepte l'identifiant de corrélation entrant et le renvoie.

    C'est le point que le middleware NestJS ne fait pas encore : il génère son
    propre `X-Request-ID` sans accepter celui du client. Tant que les deux
    services ne propagent pas le même identifiant, une action mobile ne peut
    pas être suivie de bout en bout (piège n° 12 de la grille d'analyse).
    """
    correlation_id = set_correlation_id(request.headers.get(CORRELATION_HEADER))
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = correlation_id
    return response


register_exception_handlers(app)

app.include_router(health.router)
app.include_router(profile.router)
app.include_router(catalog.public)
app.include_router(catalog.admin)
app.include_router(pricing.public)
app.include_router(pricing.admin)
app.include_router(planning.router)
app.include_router(ai_test.router)
# Banc d'essai interne (FN-034). Le routeur se refuse lui-même en production.
app.include_router(console.router)
# Plateforme de pilotage (plan.md) : API admin gardées par rôle, pages refusées
# en production comme la console.
app.include_router(admin_supervision.router)
app.include_router(admin_collecte.router)
app.include_router(admin_reglages.router)
app.include_router(admin_laboratoire.router)
app.include_router(pilotage.router)


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "service": "ARISE Nutrition Service",
        "version": app.version,
        "docs": "/docs",
        "console": "/console",
        "pilotage": "/pilotage",
    }
