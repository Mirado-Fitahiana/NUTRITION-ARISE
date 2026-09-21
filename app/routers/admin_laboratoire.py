"""Laboratoire de recommandation — API (plan §8).

Rien n'est écrit en base. Le catalogue fictif et les défauts du registre
suffisent : le laboratoire fonctionne base coupée, et le dit. Le catalogue réel,
lui, exige la base.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_catalog_editor
from app.core.config import settings
from app.core.database import get_session
from app.models.catalog import Dish
from app.models.configuration import ScoringWeightSet
from app.models.enums import ActivityLevel, Allergen, DishStatus, Goal, RestrictionType, Sex
from app.routers.pilotage_commun import RoutePilotage, invalide
from app.schemas.pilotage import ComparaisonIn, SimulationIn
from app.services import evaluation, laboratoire, reglages
from app.services.recommandation import POIDS_PAR_DEFAUT, PlatCandidat
from app.services.supervision import motif_erreur

router = APIRouter(
    prefix="/api/v1/admin/lab",
    tags=["pilotage — laboratoire"],
    dependencies=[Depends(require_catalog_editor)],
    route_class=RoutePilotage,
)

#: Cible du lot 1 : en dessous, les résultats ne représentent pas le produit.
CIBLE_PLATS_PUBLIES = 180


async def _reglages_effectifs(
    session: AsyncSession,
) -> tuple[dict[str, reglages.EtatReglage], dict[str, Decimal], str, str]:
    try:
        etats = await reglages.lire_etats(session)
        actif = await session.scalar(
            select(ScoringWeightSet).where(ScoringWeightSet.is_active.is_(True))
        )
    except (DBAPIError, OSError) as exc:
        await session.rollback()
        return (
            reglages.resoudre({}),
            dict(POIDS_PAR_DEFAUT),
            "fallback-1",
            f"défauts du registre — {motif_erreur(exc)}",
        )
    if actif is None:
        return etats, dict(POIDS_PAR_DEFAUT), "fallback-1", "base"
    poids, _ = reglages.poids_depuis(actif.weights)
    return etats, poids, actif.version, "base"


async def _catalogue(session: AsyncSession, nature: str) -> tuple[list[PlatCandidat], dict[str, Any]]:
    if nature == "fictif":
        try:
            return laboratoire.charger_catalogue_fictif()
        except laboratoire.CatalogueInvalide as exc:
            raise invalide(str(exc)) from exc

    # Mêmes fonctions que la génération réelle : le laboratoire ne lit pas le
    # catalogue autrement que le moteur.
    from app.routers.planning import _catalogue_publie, _plat_candidat

    plats = await _catalogue_publie(session)
    return [_plat_candidat(d) for d in plats], {
        "fictif": False,
        "titre": "Catalogue publié en base",
        "avertissement": None,
        "total": len(plats),
    }


async def _simuler(
    session: AsyncSession,
    demande: SimulationIn,
    *,
    poids_variante: dict[str, Any] | None = None,
    seed_variante: int | None = None,
) -> dict[str, Any]:
    plats, info = await _catalogue(session, demande.catalogue)
    etats, poids_actifs, version, source = await _reglages_effectifs(session)

    try:
        regles = reglages.regles_depuis(etats, demande.regles)
        profil = laboratoire.profil_depuis(demande.profil.model_dump(mode="json"))
        brouillon = poids_variante if poids_variante is not None else demande.poids
        if brouillon is not None:
            poids, version_poids = reglages.valider_poids(brouillon), "brouillon"
        else:
            poids, version_poids = poids_actifs, version
    except (reglages.ReglageInvalide, laboratoire.ProfilInvalide) as exc:
        raise invalide(str(exc)) from exc

    seed = seed_variante if seed_variante is not None else demande.seed
    resultat = laboratoire.simuler(
        plats,
        profil,
        jours=demande.jours,
        regles=regles,
        poids=poids,
        seed=seed,
        reponse_llm=demande.reponse_llm,
    )
    return {
        "catalogue": info,
        "profil": profil.cible,
        "regles": {
            "source": source + (" + surcharges" if demande.regles else ""),
            "valeurs": laboratoire.vue_regles(regles),
        },
        "poids": {"version": version_poids, "valeurs": reglages.affichage(poids)},
        "seed": seed,
        "jours": demande.jours,
        **resultat,
    }


@router.get("/options", summary="Listes, catalogue fictif, réglages effectifs et cas")
async def options(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    plats, info = laboratoire.charger_catalogue_fictif()
    etats, poids, version, source = await _reglages_effectifs(session)

    publies = None
    if source == "base":
        try:
            publies = await session.scalar(
                select(func.count()).select_from(Dish).where(Dish.status == DishStatus.PUBLISHED)
            )
        except (DBAPIError, OSError):
            await session.rollback()

    return {
        "enums": {
            "goals": [g.value for g in Goal],
            "allergens": [a.value for a in Allergen],
            "restrictions": [r.value for r in RestrictionType],
            "sexes": [s.value for s in Sex],
            "activity_levels": [a.value for a in ActivityLevel],
        },
        "catalogue_fictif": {
            **info,
            "ingredients": laboratoire.ingredients_du_catalogue(plats),
        },
        "plats_publies": publies,
        "cible_plats_publies": CIBLE_PLATS_PUBLIES,
        "regles": {"source": source, "valeurs": laboratoire.vue_regles(reglages.regles_depuis(etats))},
        "surchargeables": [
            reglages.vue_etat(etats[r.cle])
            for r in reglages.REGISTRE
            if r.groupe == reglages.GROUPE_COMPOSITION and r.branche and r.type != reglages.REPARTITION
        ],
        "poids": {
            "version": version,
            "valeurs": reglages.affichage(poids),
            "libelles": reglages.POIDS_LIBELLES,
            "maximum": str(reglages.POIDS_MAX),
        },
        "cas": [
            {"id": c.id, "titre": c.titre, "description": c.description, "fichier": c.fichier}
            for c in evaluation.charger_cas()
        ],
        "llm": {
            "statut": "desactive",
            "motif": laboratoire.MOTIF_LLM_DESACTIVE,
            "modele": settings.llm_model,
        },
    }


@router.post("/simulate", summary="Dérouler la chaîne pas à pas, sans rien écrire")
async def simulate(payload: SimulationIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await _simuler(session, payload)


@router.post("/compare", summary="Comparer deux jeux de poids ou deux graines")
async def compare(payload: ComparaisonIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if payload.variante_poids is None and payload.variante_seed is None:
        raise invalide("Indiquer un jeu de poids ou une graine pour la variante.")
    a = await _simuler(session, payload.base)
    b = await _simuler(
        session, payload.base, poids_variante=payload.variante_poids, seed_variante=payload.variante_seed
    )
    return {"a": a, "b": b, "comparaison": laboratoire.comparer(a, b)}


@router.get("/cases", summary="Cas d'évaluation versionnés")
async def cases() -> dict[str, Any]:
    return {
        "cas": [
            {
                "id": c.id,
                "titre": c.titre,
                "description": c.description,
                "fichier": c.fichier,
                "jours": c.jours,
                "attendu": c.attendu,
            }
            for c in evaluation.charger_cas()
        ]
    }


@router.post("/evaluations", summary="Rejouer tous les cas — mêmes verdicts que pytest")
async def evaluations() -> dict[str, Any]:
    plats, _ = laboratoire.charger_catalogue_fictif()
    return evaluation.executer_campagne(evaluation.charger_cas(), plats)
