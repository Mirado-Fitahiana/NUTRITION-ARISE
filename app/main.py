"""Point d'entrée du service Nutrition ARISE.

Service autonome (§4.2) : base dédiée, migrations propres, aucun appel
synchrone vers NestJS pendant un traitement nutritionnel.
"""

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
from app.routers import ai_test, catalog, console, health, profile


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
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
app.include_router(ai_test.router)
# Banc d'essai interne (FN-034). Le routeur se refuse lui-même en production.
app.include_router(console.router)


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "service": "ARISE Nutrition Service",
        "version": app.version,
        "docs": "/docs",
        "console": "/console",
    }
