"""Plateforme de pilotage — pages HTML (FN-034, plan §4).

Les pages ne portent aucune logique : elles sont des **clientes des API admin**
(`/api/v1/admin/...`). Ce qui est vérifié par l'API l'est donc pour la page.

Comme `/console`, la plateforme n'existe pas en production : 404, et non 403.
Les fichiers statiques passent par ce même routeur plutôt que par un
`StaticFiles` monté à part, qui échapperait à cette garde.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.routers.console import dev_only

APP_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = (APP_DIR / "static" / "pilotage").resolve()
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

TYPES_MEDIA = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

router = APIRouter(
    prefix="/pilotage",
    tags=["pilotage"],
    dependencies=[Depends(dev_only)],
    include_in_schema=False,
)

NAVIGATION = (
    {"id": "supervision", "href": "/pilotage", "libelle": "Supervision", "icone": "dashboard", "ref": "FN-035"},
    {"id": "collecte", "href": "/pilotage/collecte", "libelle": "Collecte", "icone": "collecte", "ref": "FN-013"},
    {"id": "laboratoire", "href": "/pilotage/laboratoire", "libelle": "Laboratoire", "icone": "laboratoire", "ref": "FN-019 → 022"},
    {"id": "reglages", "href": "/pilotage/reglages", "libelle": "Réglages", "icone": "reglages", "ref": "FN-020"},
)


def _version_statique() -> str:
    """Empreinte des fichiers statiques, pour que le navigateur ne serve pas
    une ancienne version après une modification."""
    try:
        return str(int(max(p.stat().st_mtime for p in STATIC_DIR.rglob("*") if p.is_file())))
    except ValueError:
        return "0"


def _page(request: Request, gabarit: str, page: str, titre: str, sous_titre: str, **extra) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        f"pilotage/{gabarit}",
        {
            "page": page,
            "titre": titre,
            "sous_titre": sous_titre,
            "navigation": NAVIGATION,
            "environnement": settings.environment,
            "v": _version_statique(),
            **extra,
        },
    )


@router.get("", response_class=HTMLResponse)
async def page_supervision(request: Request) -> HTMLResponse:
    return _page(
        request,
        "supervision.html",
        "supervision",
        "Supervision",
        "Ce que le service fait réellement — mesuré à chaque affichage, jamais déclaré.",
    )


@router.get("/collecte", response_class=HTMLResponse)
async def page_collecte(request: Request) -> HTMLResponse:
    return _page(
        request,
        "collecte.html",
        "collecte",
        "Collecte de prix",
        "Relevés manuels, suivis en direct. Aucun prix n'est écrit avant le feu vert du lot 4.",
    )


@router.get("/collecte/executions/{run_id}", response_class=HTMLResponse)
async def page_execution(request: Request, run_id: str) -> HTMLResponse:
    try:
        identifiant = str(uuid.UUID(run_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found") from exc
    return _page(
        request,
        "execution.html",
        "collecte",
        "Exécution",
        "Progression, journal, offres extraites et erreurs.",
        run_id=identifiant,
    )


@router.get("/laboratoire", response_class=HTMLResponse)
async def page_laboratoire(request: Request) -> HTMLResponse:
    return _page(
        request,
        "laboratoire.html",
        "laboratoire",
        "Laboratoire de recommandation",
        "La chaîne filtrage → score → composition → validation → rédaction, étape par étape.",
    )


@router.get("/reglages", response_class=HTMLResponse)
async def page_reglages(request: Request) -> HTMLResponse:
    return _page(
        request,
        "reglages.html",
        "reglages",
        "Réglages",
        "Paramètres métier et poids du score — la valeur affichée est celle que le moteur utilise.",
    )


@router.get("/static/{chemin:path}")
async def fichier_statique(chemin: str) -> FileResponse:
    cible = (STATIC_DIR / chemin).resolve()
    if not cible.is_relative_to(STATIC_DIR) or not cible.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return FileResponse(
        cible,
        media_type=TYPES_MEDIA.get(cible.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "no-cache"},
    )
