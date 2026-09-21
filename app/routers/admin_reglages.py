"""Réglages — API de la plateforme de pilotage (FN-020, FN-024, plan §7).

La lecture reste possible base coupée : le registre et ses défauts s'affichent,
avec le motif de l'indisponibilité. L'écriture, elle, exige la base — et le rôle
administrateur.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_admin, require_catalog_editor
from app.core.config import settings
from app.core.database import get_session
from app.models.audit import AuditAction
from app.models.configuration import AppSetting, ScoringWeightSet
from app.models.planning import RecommendationRun
from app.routers.pilotage_commun import RoutePilotage, conflit, introuvable, invalide
from app.schemas.pilotage import SettingIn, WeightSetIn
from app.services import audit, reglages
from app.services.collecte.politesse import contact_ascii, user_agent
from app.services.recommandation import POIDS_PAR_DEFAUT
from app.services.supervision import motif_erreur, tete_alembic

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["pilotage — réglages"],
    dependencies=[Depends(require_catalog_editor)],
    route_class=RoutePilotage,
)

VERSION_REPLI = "fallback-1"


# --------------------------------------------------------------------------
# Paramètres opérationnels
# --------------------------------------------------------------------------


@router.get("/settings", summary="Registre des réglages : stocké, effectif, défaut")
async def get_settings(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    motif = None
    try:
        lignes = await reglages.lire_lignes(session)
    except (DBAPIError, OSError) as exc:
        await session.rollback()
        lignes, motif = {}, motif_erreur(exc)

    etats = reglages.resoudre(lignes)
    return {
        "base_joignable": motif is None,
        "motif": motif,
        "groupes": list(dict.fromkeys(r.groupe for r in reglages.REGISTRE)),
        "reglages": [reglages.vue_etat(etats[r.cle]) for r in reglages.REGISTRE],
        "cles_inconnues": reglages.cles_inconnues(lignes),
    }


@router.put("/settings/{cle}", summary="Modifier un réglage branché")
async def put_setting(
    cle: str,
    payload: SettingIn,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    reglage = reglages.PAR_CLE.get(cle)
    if reglage is None:
        raise introuvable(f"Réglage inconnu : {cle}.")
    if not reglage.branche:
        raise conflit(
            f"« {reglage.libelle} » n'est lu par aucun traitement ({reglage.lu_par}) : "
            "l'enregistrer n'aurait aucun effet."
        )
    try:
        valeur = reglages.valider(reglage, payload.value)
    except reglages.ReglageInvalide as exc:
        raise invalide(f"{reglage.libelle} : {exc}") from exc

    ligne = await session.get(AppSetting, cle)
    avant = reglages.extraire(ligne.value) if ligne else None
    maintenant = datetime.now(UTC)
    if ligne is None:
        session.add(
            AppSetting(
                key=cle,
                value=reglages.en_json(reglage, valeur),
                description=reglage.libelle,
                updated_at=maintenant,
                updated_by=principal.external_user_id,
            )
        )
    else:
        ligne.value = reglages.en_json(reglage, valeur)
        ligne.updated_at = maintenant
        ligne.updated_by = principal.external_user_id

    await audit.record(
        session,
        AuditAction.SETTING_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="app_setting",
        resource_id=cle,
        details={"cle": cle, "avant": avant, "apres": reglages.affichage(valeur)},
    )
    await session.flush()
    etats = await reglages.lire_etats(session)
    return reglages.vue_etat(etats[cle])


@router.delete("/settings/{cle}", summary="Revenir à la valeur par défaut")
async def reset_setting(
    cle: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ligne = await session.get(AppSetting, cle)
    if ligne is None:
        raise introuvable(f"Aucune valeur stockée pour « {cle} » : le défaut s'applique déjà.")
    avant = reglages.extraire(ligne.value)
    await session.delete(ligne)
    await audit.record(
        session,
        AuditAction.SETTING_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="app_setting",
        resource_id=cle,
        details={"cle": cle, "avant": avant, "apres": "défaut"},
    )
    await session.flush()
    etats = await reglages.lire_etats(session)
    return reglages.vue_etat(etats[cle]) if cle in etats else {"cle": cle, "supprime": True}


# --------------------------------------------------------------------------
# Jeux de poids du score
# --------------------------------------------------------------------------


def _vue_jeu(jeu: ScoringWeightSet, runs: dict[str, int]) -> dict[str, Any]:
    effectifs, ignores = reglages.poids_depuis(jeu.weights)
    return {
        "version": jeu.version,
        "description": jeu.description,
        "weights": reglages.affichage(jeu.weights or {}),
        "effectifs": reglages.affichage(effectifs),
        "ignores": ignores,
        "is_active": jeu.is_active,
        "created_at": jeu.created_at.isoformat() if jeu.created_at else None,
        "runs": runs.get(jeu.version, 0),
    }


@router.get("/scoring-weights", summary="Jeux de poids versionnés")
async def list_weights(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    motif = None
    jeux: list[ScoringWeightSet] = []
    runs: dict[str, int] = {}
    try:
        jeux = list(
            await session.scalars(select(ScoringWeightSet).order_by(ScoringWeightSet.created_at.desc()))
        )
        runs = {
            version: n
            for version, n in (
                await session.execute(
                    select(RecommendationRun.scoring_version, func.count()).group_by(
                        RecommendationRun.scoring_version
                    )
                )
            ).all()
        }
    except (DBAPIError, OSError) as exc:
        await session.rollback()
        motif = motif_erreur(exc)

    actif = next((j for j in jeux if j.is_active), None)
    return {
        "base_joignable": motif is None,
        "motif": motif,
        "autorises": [
            {"cle": c, "libelle": reglages.POIDS_LIBELLES[c], "defaut": str(POIDS_PAR_DEFAUT[c])}
            for c in reglages.POIDS_AUTORISES
        ],
        "prevus": [{"cle": c, "libelle": l} for c, l in reglages.POIDS_PREVUS.items()],
        "maximum": str(reglages.POIDS_MAX),
        "actif": actif.version if actif else VERSION_REPLI,
        "repli": {
            "version": VERSION_REPLI,
            "weights": reglages.affichage(POIDS_PAR_DEFAUT),
            "is_active": actif is None,
            "runs": runs.get(VERSION_REPLI, 0),
        },
        "jeux": [_vue_jeu(j, runs) for j in jeux],
    }


@router.post("/scoring-weights", status_code=status.HTTP_201_CREATED, summary="Créer une version (inactive)")
async def create_weights(
    payload: WeightSetIn,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        version = reglages.valider_version(payload.version)
        poids = reglages.valider_poids(payload.weights)
    except reglages.ReglageInvalide as exc:
        raise invalide(str(exc)) from exc

    if await session.scalar(select(ScoringWeightSet).where(ScoringWeightSet.version == version)):
        raise conflit(f"La version « {version} » existe déjà : une version ne se modifie pas.")

    jeu = ScoringWeightSet(
        version=version,
        description=payload.description or None,
        weights=reglages.affichage(poids),
        is_active=False,
    )
    session.add(jeu)
    await audit.record(
        session,
        AuditAction.SCORING_WEIGHTS_CREATED,
        external_user_id=principal.external_user_id,
        resource_type="scoring_weight_set",
        resource_id=version,
        details={"version": version, "poids": reglages.affichage(poids)},
    )
    await session.flush()
    await session.refresh(jeu)
    return _vue_jeu(jeu, {})


@router.post("/scoring-weights/{version}/activate", summary="Activer une version")
async def activate_weights(
    version: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    jeu = await session.scalar(select(ScoringWeightSet).where(ScoringWeightSet.version == version))
    if jeu is None:
        raise introuvable(f"Aucune version « {version} ».")
    _, ignores = reglages.poids_depuis(jeu.weights)
    if ignores:
        raise conflit(
            f"Cette version contient des clés que le moteur ignore ({', '.join(ignores)}) : "
            "créer une version corrigée plutôt que de l'activer."
        )

    if not jeu.is_active:
        # Un seul jeu actif : l'index unique partiel l'impose, il faut désactiver d'abord.
        await session.execute(
            update(ScoringWeightSet).where(ScoringWeightSet.is_active.is_(True)).values(is_active=False)
        )
        await session.flush()
        jeu.is_active = True
        await audit.record(
            session,
            AuditAction.SCORING_WEIGHTS_ACTIVATED,
            external_user_id=principal.external_user_id,
            resource_type="scoring_weight_set",
            resource_id=version,
            details={"version": version},
        )
        await session.flush()
    await session.refresh(jeu)
    return _vue_jeu(jeu, {})


@router.post("/scoring-weights/fallback", summary="Désactiver tout jeu : retour aux poids de repli")
async def fallback_weights(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    resultat = await session.execute(
        update(ScoringWeightSet).where(ScoringWeightSet.is_active.is_(True)).values(is_active=False)
    )
    await audit.record(
        session,
        AuditAction.SCORING_WEIGHTS_ACTIVATED,
        external_user_id=principal.external_user_id,
        resource_type="scoring_weight_set",
        resource_id=VERSION_REPLI,
        details={"version": VERSION_REPLI, "desactives": resultat.rowcount or 0},
    )
    await session.flush()
    return {"actif": VERSION_REPLI, "desactives": resultat.rowcount or 0}


# --------------------------------------------------------------------------
# Environnement
# --------------------------------------------------------------------------


@router.get("/environment", summary="Configuration d'environnement, en lecture seule")
async def environment(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.services.progress import _jwks_reachable

    revision, motif = None, None
    try:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    except (DBAPIError, OSError) as exc:
        await session.rollback()
        motif = motif_erreur(exc)
    tete = tete_alembic()
    jwks_ok, jwks_detail = await _jwks_reachable()

    return {
        "service": {
            "environnement": settings.environment,
            "niveau_journal": settings.log_level,
            "ecoute": f"{settings.host}:{settings.port}",
            "expose_reseau": settings.host in ("0.0.0.0", "::"),
        },
        "base": {
            "hote": settings.database_host,
            "port": settings.database_port,
            "nom": settings.database_name,
            "utilisateur": settings.database_user,
            "mot_de_passe_configure": bool(settings.database_password.get_secret_value()),
            "joignable": motif is None,
            "motif": motif,
            "revision": revision,
            "tete": tete,
            "a_jour": revision is not None and revision == tete,
        },
        "authentification": {
            "jwks_url": settings.jwt_jwks_url,
            "emetteur": settings.jwt_issuer,
            "audience": settings.jwt_audience,
            "jwks_joignable": jwks_ok,
            "jwks_detail": jwks_detail,
            "cle_dev_publique": bool(settings.jwt_dev_public_key_path),
            "cle_dev_privee": bool(settings.jwt_dev_private_key_path),
        },
        "llm": {
            "modele": settings.llm_model,
            "cle_configuree": bool(settings.open_router_key.get_secret_value()),
            "delai_s": settings.llm_timeout_seconds,
        },
        "observabilite": {"sentry_configure": bool(settings.sentry_dsn)},
        "collecte": {
            "agent": user_agent(settings.scraping_contact),
            "contact_renseigne": bool(contact_ascii(settings.scraping_contact)),
            "dossier_instantanes": settings.scraping_snapshot_dir,
        },
    }
