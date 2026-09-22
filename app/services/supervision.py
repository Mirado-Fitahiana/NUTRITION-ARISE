"""Tableau de bord de supervision — FN-035, mesuré (plan §6).

Même principe que `app.services.progress` : **rien n'est déclaré.** Chaque
indicateur porte une valeur, un état et **sa preuve** — la requête ou la sonde
qui l'a produit.

Deux règles d'affichage, vérifiées par `tests/test_supervision.py` :

* **« aucune donnée » n'est jamais un zéro.** Un indicateur qu'on ne sait pas
  mesurer est `inconnu`. Un compte mesuré à zéro, lui, est un vrai zéro ;
* **une mesure qui échoue n'emporte pas le tableau.** Chaque bloc est isolé :
  une table absente (migration non appliquée) rend son bloc `inconnu` avec le
  motif, les autres restent lisibles.

L'état des indicateurs est calculé par des fonctions pures, en haut du module.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.catalog import Dish, DishIngredient, Ingredient
from app.models.enums import DishStatus
from app.services.catalog import publication_blockers
from app.services.pricing import SEUIL_ANCIEN_JOURS, SEUIL_RECENT_JOURS
from app.services.units import UnitConversionError

logger = logging.getLogger("app.supervision")

ROOT = Path(__file__).resolve().parents[2]

OK, ALERTE, CRITIQUE, INCONNU = "ok", "alerte", "critique", "inconnu"


@dataclass
class Indicateur:
    id: str
    libelle: str
    valeur: Any
    etat: str
    preuve: str
    unite: str | None = None
    detail: str | None = None
    #: Série temporelle : [{"x": "2026-09-14", "y": 3, ...}].
    serie: list[dict[str, Any]] | None = None
    #: Répartition : [{"libelle": "...", "valeur": 3}].
    repartition: list[dict[str, Any]] | None = None


@dataclass
class Bloc:
    id: str
    titre: str
    description: str
    indicateurs: list[Indicateur] = field(default_factory=list)
    erreur: str | None = None


# --------------------------------------------------------------------------
# États — fonctions pures
# --------------------------------------------------------------------------


def etat_incidents(nombre: int | None) -> str:
    """FN-035 — toute valeur non nulle est un incident de production."""
    if nombre is None:
        return INCONNU
    return OK if nombre == 0 else CRITIQUE


def etat_non_verifies(total: int | None, non_verifies: int | None) -> str:
    """« Doit être 0 » — mais un catalogue en cours de signature est attendu :
    alerte, pas critique."""
    if total is None or non_verifies is None:
        return INCONNU
    if total == 0:
        return INCONNU
    return OK if non_verifies == 0 else ALERTE


def etat_taux_echec(echecs: int | None, total: int | None, *, seuil: float = 0.5) -> str:
    """Zéro génération sur la période : le taux n'existe pas, il est inconnu."""
    if echecs is None or not total:
        return INCONNU
    return ALERTE if echecs / total > seuil else OK


def etat_migration(revision_base: str | None, tete: str | None) -> str:
    if revision_base is None or tete is None:
        return INCONNU
    return OK if revision_base == tete else ALERTE


def etat_couverture(ratio: float | None, *, seuil: float = 0.5) -> str:
    if ratio is None:
        return INCONNU
    return OK if ratio >= seuil else ALERTE


def etat_compte_problemes(nombre: int | None) -> str:
    if nombre is None:
        return INCONNU
    return OK if nombre == 0 else ALERTE


def ratio(numerateur: int | None, denominateur: int | None) -> float | None:
    if numerateur is None or not denominateur:
        return None
    return round(numerateur / denominateur, 4)


def serie_journaliere(
    valeurs: dict[date, dict[str, Any]], debut: date, fin: date, cles: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Complète les jours sans ligne par des zéros **mesurés** : la table existe
    et n'a rien enregistré ce jour-là."""
    serie: list[dict[str, Any]] = []
    jour = debut
    while jour <= fin:
        ligne = valeurs.get(jour, {})
        serie.append({"x": jour.isoformat(), **{c: ligne.get(c, 0) for c in cles}})
        jour += timedelta(days=1)
    return serie


def motif_erreur(exc: BaseException) -> str:
    texte = str(getattr(exc, "orig", None) or exc)
    bas = texte.lower()
    if "does not exist" in bas or "n'existe pas" in bas:
        return "table absente — appliquer `alembic upgrade head`"
    if any(m in bas for m in ("connection", "connexion", "authentification", "authentication")):
        return "base injoignable — vérifier DATABASE_* dans .env"
    return f"mesure impossible ({type(exc).__name__})"


def resume(blocs: list[Bloc]) -> dict[str, int]:
    etats = Counter(i.etat for b in blocs for i in b.indicateurs)
    return {
        "critiques": etats.get(CRITIQUE, 0),
        "alertes": etats.get(ALERTE, 0),
        "inconnus": etats.get(INCONNU, 0),
        "ok": etats.get(OK, 0),
    }


# --------------------------------------------------------------------------
# Sondes utilitaires
# --------------------------------------------------------------------------


def tete_alembic() -> str | None:
    """Révision la plus récente des fichiers de migration."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # noqa: BLE001
        return None


async def _lignes(session: AsyncSession, sql: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(sql), params)).all())


async def _scalaire(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _proteger(
    session: AsyncSession,
    bloc: Bloc,
    mesure: Callable[[AsyncSession], Awaitable[list[Indicateur]]],
) -> Bloc:
    try:
        bloc.indicateurs = await mesure(session)
    except Exception as exc:  # noqa: BLE001 — le bloc affiche la cause
        logger.warning("bloc %s non mesuré : %s", bloc.id, exc)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        bloc.erreur = motif_erreur(exc)
        bloc.indicateurs = [
            Indicateur(f"{bloc.id}.indisponible", "Mesure indisponible", None, INCONNU, bloc.erreur)
        ]
    return bloc


# --------------------------------------------------------------------------
# Blocs
# --------------------------------------------------------------------------


def _mesure_securite(depuis: datetime):
    async def mesurer(session: AsyncSession) -> list[Indicateur]:
        total, periode = (
            await _lignes(
                session,
                "SELECT count(*) FILTER (WHERE is_food_safety_incident),"
                " count(*) FILTER (WHERE is_food_safety_incident AND created_at >= :depuis)"
                " FROM plan_validations",
                depuis=depuis,
            )
        )[0]
        allergies = await _scalaire(
            session,
            "SELECT count(*) FROM user_meal_feedback WHERE feedback_type = 'allergy_issue'",
        )
        return [
            Indicateur(
                "securite.incidents",
                "Incidents de sécurité alimentaire (validation finale)",
                total,
                etat_incidents(total),
                "plan_validations.is_food_safety_incident — depuis l'origine",
                detail=f"{periode} sur la période",
            ),
            Indicateur(
                "securite.allergies",
                "Signalements d'allergie par les utilisateurs",
                allergies,
                etat_incidents(allergies),
                "user_meal_feedback · feedback_type = allergy_issue",
            ),
        ]

    return mesurer


async def _mesure_sante(session: AsyncSession) -> list[Indicateur]:
    from app.services.progress import _jwks_reachable

    revision = await _scalaire(session, "SELECT version_num FROM alembic_version")
    tete = tete_alembic()
    jwks_ok, jwks_detail = await _jwks_reachable()
    cle_llm = bool(settings.open_router_key.get_secret_value())

    return [
        Indicateur(
            "sante.base",
            "Base de données",
            "joignable",
            OK,
            f"SELECT sur {settings.database_name}",
        ),
        Indicateur(
            "sante.migration",
            "Migrations appliquées",
            revision,
            etat_migration(revision, tete),
            f"alembic_version = {revision} · tête des fichiers = {tete}",
            detail=None if revision == tete else "Appliquer `alembic upgrade head`",
        ),
        Indicateur(
            "sante.jwks",
            "Fournisseur d'identité (JWKS NestJS)",
            "joignable" if jwks_ok else "injoignable",
            OK if jwks_ok else ALERTE,
            f"GET {settings.jwt_jwks_url} — {jwks_detail}",
            detail=None if jwks_ok else "Attendu tant que NestJS n'a pas migré en RS256 (sprint 01)",
        ),
        Indicateur(
            "sante.llm",
            "Clé du fournisseur IA",
            "configurée" if cle_llm else "absente",
            OK,
            "OPEN_ROUTER_KEY (valeur jamais affichée)",
            detail="Sans objet en v1 : aucun appel LLM en génération",
        ),
        Indicateur(
            "sante.ecoute",
            "Adresse d'écoute",
            settings.host,
            ALERTE if settings.host in ("0.0.0.0", "::") else OK,
            "HOST (run.py)",
            detail=(
                "Exposée au réseau local : n'importe qui peut émettre un jeton de développement"
                if settings.host in ("0.0.0.0", "::")
                else None
            ),
        ),
    ]


async def _mesure_catalogue(session: AsyncSession) -> list[Indicateur]:
    par_statut = await _lignes(session, "SELECT status, count(*) FROM dishes GROUP BY status")
    total_ing, verifies = (
        await _lignes(
            session,
            "SELECT count(*), count(*) FILTER (WHERE allergen_source IS NOT NULL"
            " AND allergen_verified_by IS NOT NULL AND allergen_verified_at IS NOT NULL)"
            " FROM ingredients",
        )
    )[0]
    non_indexes = await _scalaire(
        session,
        "SELECT count(*) FROM dishes d WHERE d.status = 'published' AND NOT EXISTS"
        " (SELECT 1 FROM dish_embeddings e WHERE e.dish_id = d.id AND e.indexed_at IS NOT NULL)",
    )

    plats = (
        await session.scalars(
            select(Dish)
            .where(Dish.status.not_in([DishStatus.PUBLISHED, DishStatus.ARCHIVED]))
            .options(
                selectinload(Dish.ingredients)
                .selectinload(DishIngredient.ingredient)
                .selectinload(Ingredient.unit_conversions),
                selectinload(Dish.tags),
                selectinload(Dish.allergens),
            )
        )
    ).all()
    motifs: Counter[str] = Counter()
    for plat in plats:
        try:
            bloquants = publication_blockers(plat)
        except UnitConversionError:
            bloquants = ["conversion d'unité manquante"]
        if bloquants:
            # Le premier motif est celui qu'un rédacteur rencontre d'abord.
            motifs[bloquants[0].split(" pour :")[0]] += 1

    repartition = [{"libelle": str(s), "valeur": n} for s, n in par_statut]
    publies = sum(n for s, n in par_statut if str(s) == "published")
    non_verifies = (total_ing or 0) - (verifies or 0)
    return [
        Indicateur(
            "catalogue.plats",
            "Plats par statut",
            sum(n for _, n in par_statut),
            OK if publies else ALERTE,
            "SELECT status, count(*) FROM dishes",
            detail=f"{publies} publié(s) — cible du lot 1 : ~180",
            repartition=repartition,
        ),
        Indicateur(
            "catalogue.non_verifies",
            "Ingrédients aux allergènes non signés",
            non_verifies,
            etat_non_verifies(total_ing, non_verifies),
            f"{verifies}/{total_ing} ingrédients signés (source, validateur, date)",
            detail="Doit finir à 0 — attendu tant que le nutritionniste n'a pas signé" if non_verifies else None,
        ),
        Indicateur(
            "catalogue.non_publiables",
            "Plats non publiables",
            sum(motifs.values()),
            etat_compte_problemes(sum(motifs.values())),
            "publication_blockers() rejoué sur les plats non publiés",
            repartition=[{"libelle": m, "valeur": n} for m, n in motifs.most_common()],
        ),
        Indicateur(
            "catalogue.non_indexes",
            "Plats publiés non indexés",
            non_indexes,
            OK,
            "dishes publiés sans dish_embeddings.indexed_at",
            detail="Lot 4 différé : attendu",
        ),
    ]


def _mesure_generation(depuis: datetime, debut: date, fin: date):
    async def mesurer(session: AsyncSession) -> list[Indicateur]:
        par_statut = dict(
            await _lignes(
                session,
                "SELECT status, count(*) FROM generation_jobs WHERE created_at >= :depuis GROUP BY status",
                depuis=depuis,
            )
        )
        total = sum(par_statut.values())
        echecs = par_statut.get("failed", 0)

        par_jour = {
            ligne[0].date(): {"total": ligne[1], "echecs": ligne[2]}
            for ligne in await _lignes(
                session,
                "SELECT date_trunc('day', created_at), count(*),"
                " count(*) FILTER (WHERE status = 'failed')"
                " FROM generation_jobs WHERE created_at >= :depuis GROUP BY 1",
                depuis=depuis,
            )
        }
        duree = await _scalaire(
            session,
            "SELECT avg(extract(epoch FROM (finished_at - started_at)))"
            " FROM generation_jobs WHERE status = 'succeeded' AND started_at IS NOT NULL"
            " AND finished_at IS NOT NULL AND created_at >= :depuis",
            depuis=depuis,
        )
        codes = await _lignes(
            session,
            "SELECT coalesce(error_code, 'sans code'), count(*) FROM generation_jobs"
            " WHERE status = 'failed' AND created_at >= :depuis GROUP BY 1 ORDER BY 2 DESC",
            depuis=depuis,
        )
        controles = await _lignes(
            session,
            "SELECT c, count(*) FROM plan_validations, unnest(failed_checks) AS c"
            " WHERE created_at >= :depuis GROUP BY c ORDER BY c",
            depuis=depuis,
        )
        runs, repli = (
            await _lignes(
                session,
                "SELECT count(*), count(*) FILTER (WHERE used_fallback)"
                " FROM recommendation_runs WHERE created_at >= :depuis",
                depuis=depuis,
            )
        )[0]

        return [
            Indicateur(
                "generation.volume",
                "Générations lancées",
                total,
                OK,
                "generation_jobs sur la période",
                serie=serie_journaliere(par_jour, debut, fin, ("total", "echecs")),
            ),
            Indicateur(
                "generation.echecs",
                "Taux d'échec",
                ratio(echecs, total),
                etat_taux_echec(echecs, total),
                f"{echecs} échec(s) sur {total}",
                unite="ratio",
                detail="Aucune génération sur la période" if not total else None,
                repartition=[{"libelle": c, "valeur": n} for c, n in codes],
            ),
            Indicateur(
                "generation.duree",
                "Durée moyenne d'une génération réussie",
                round(float(duree), 3) if duree is not None else None,
                OK if duree is not None else INCONNU,
                "moyenne de finished_at − started_at",
                unite="s",
            ),
            Indicateur(
                "generation.rejets",
                "Rejets de la validation finale, par contrôle",
                sum(n for _, n in controles),
                etat_compte_problemes(sum(n for _, n in controles)),
                "unnest(plan_validations.failed_checks)",
                repartition=[{"libelle": f"contrôle {c}", "valeur": n} for c, n in controles],
            ),
            Indicateur(
                "generation.repli",
                "Générations en texte de repli",
                ratio(repli, runs),
                OK if runs else INCONNU,
                f"{repli}/{runs} recommendation_runs.used_fallback",
                unite="ratio",
                detail="v1 : 100 % attendu, aucun appel LLM" if runs else "Aucun run sur la période",
            ),
        ]

    return mesurer


def _mesure_cout(depuis: datetime, debut: date, fin: date):
    async def mesurer(session: AsyncSession) -> list[Indicateur]:
        lignes = await _lignes(
            session,
            "SELECT date_trunc('day', created_at), sum(llm_call_count),"
            " sum(input_tokens + output_tokens), sum(cost), count(*)"
            " FROM recommendation_runs WHERE created_at >= :depuis GROUP BY 1",
            depuis=depuis,
        )
        par_jour = {
            l[0].date(): {"appels": int(l[1] or 0), "tokens": int(l[2] or 0), "cout": float(l[3] or 0)}
            for l in lignes
        }
        appels = sum(v["appels"] for v in par_jour.values())
        tokens = sum(v["tokens"] for v in par_jour.values())
        cout = sum(v["cout"] for v in par_jour.values())
        programmes = sum(int(l[4] or 0) for l in lignes)
        return [
            Indicateur(
                "cout.appels",
                "Appels LLM",
                appels,
                OK,
                "sum(recommendation_runs.llm_call_count)",
                detail=f"{tokens} tokens",
                serie=serie_journaliere(par_jour, debut, fin, ("appels", "tokens", "cout")),
            ),
            Indicateur(
                "cout.total",
                "Coût IA sur la période",
                round(cout, 4),
                OK,
                "sum(recommendation_runs.cost)",
                unite="USD",
            ),
            Indicateur(
                "cout.par_programme",
                "Coût moyen par programme",
                round(cout / programmes, 6) if programmes else None,
                OK if programmes else INCONNU,
                f"{programmes} programme(s)",
                unite="USD",
            ),
            Indicateur(
                "cout.quotas",
                "Utilisateurs ayant atteint leur quota",
                None,
                INCONNU,
                "aucune action d'audit ne trace les refus de quota (QUOTA_EXCEEDED)",
            ),
        ]

    return mesurer


#: Repas réellement proposés. Un remplacement (FN-025) crée une nouvelle version
#: du programme qui recopie tous les repas : compter les versions archivées
#: doublerait chaque plat. On garde les repas des versions en vigueur, plus,
#: dans les versions archivées, le seul repas remplacé — lui a bien été proposé.
REPAS_PROPOSES = (
    "meal_plan_meals m"
    " JOIN meal_plan_days d ON d.id = m.day_id"
    " JOIN meal_plans p ON p.id = d.plan_id"
    " WHERE (p.status <> 'archived' OR m.tracked_status = 'replaced')"
)


def taux_acceptation(suivis: dict[str, int]) -> float | None:
    """Repas suivis parmi ceux sur lesquels l'utilisateur s'est prononcé.

    Un repas encore `pending` n'est ni accepté ni refusé : il n'entre pas au
    dénominateur, sinon un programme tout juste généré ferait chuter le taux.
    """
    suivi = suivis.get("followed", 0)
    tranches = suivi + suivis.get("skipped", 0) + suivis.get("replaced", 0)
    return ratio(suivi, tranches)


def _mesure_qualite(depuis: datetime):
    async def mesurer(session: AsyncSession) -> list[Indicateur]:
        retours = dict(
            await _lignes(
                session,
                "SELECT feedback_type, count(*) FROM user_meal_feedback"
                " WHERE created_at >= :depuis GROUP BY 1",
                depuis=depuis,
            )
        )
        suivis = {
            str(k): v
            for k, v in await _lignes(
                session,
                f"SELECT m.tracked_status, count(*) FROM {REPAS_PROPOSES}"
                " AND d.day_date >= :depuis_jour GROUP BY 1",
                depuis_jour=depuis.date(),
            )
        }
        top_suivis = await _lignes(
            session,
            f"SELECT m.dish_snapshot->>'name', count(*) FROM {REPAS_PROPOSES}"
            " AND m.tracked_status = 'followed' GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
        )
        top_proposes = await _lignes(
            session,
            f"SELECT m.dish_snapshot->>'name', count(*) FROM {REPAS_PROPOSES}"
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
        )
        top_remplaces = await _lignes(
            session,
            f"SELECT m.dish_snapshot->>'name', count(*) FROM {REPAS_PROPOSES}"
            " AND m.tracked_status = 'replaced' GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
        )
        total = sum(suivis.values())
        remplaces = suivis.get("replaced", 0)
        tranches = suivis.get("followed", 0) + suivis.get("skipped", 0) + remplaces
        return [
            Indicateur(
                "qualite.acceptation",
                "Taux d'acceptation",
                taux_acceptation(suivis),
                OK if tranches else INCONNU,
                f"repas suivis / repas suivis, sautés ou remplacés — {tranches} décision(s)",
                unite="ratio",
                repartition=[{"libelle": k, "valeur": v} for k, v in sorted(suivis.items())],
            ),
            Indicateur(
                "qualite.alternatives",
                "Alternatives demandées",
                ratio(remplaces, total),
                OK if total else INCONNU,
                f"{remplaces} « Proposer autre chose » sur {total} repas proposés",
                unite="ratio",
            ),
            Indicateur(
                "qualite.retours",
                "Retours des utilisateurs",
                sum(retours.values()),
                OK if retours else INCONNU,
                "user_meal_feedback sur la période",
                repartition=[{"libelle": str(k), "valeur": v} for k, v in sorted(retours.items())],
            ),
            Indicateur(
                "qualite.top_proposes",
                "Plats les plus proposés",
                len(top_proposes),
                OK if top_proposes else INCONNU,
                "repas des programmes en vigueur (copies de versions exclues)",
                repartition=[{"libelle": nom or "—", "valeur": n} for nom, n in top_proposes],
            ),
            Indicateur(
                "qualite.top",
                "Plats les plus suivis",
                len(top_suivis),
                OK if top_suivis else INCONNU,
                "meal_plan_meals.dish_snapshot, repas suivis",
                repartition=[{"libelle": nom or "—", "valeur": n} for nom, n in top_suivis],
            ),
            Indicateur(
                "qualite.top_remplaces",
                "Plats les plus remplacés",
                len(top_remplaces),
                OK if top_remplaces else INCONNU,
                "repas remplacés par « Proposer autre chose »",
                repartition=[{"libelle": nom or "—", "valeur": n} for nom, n in top_remplaces],
            ),
        ]

    return mesurer


async def _mesure_prix(session: AsyncSession) -> list[Indicateur]:
    total = await _scalaire(session, "SELECT count(*) FROM ingredients")
    sans_prix = await _scalaire(
        session,
        "SELECT count(*) FROM ingredients i WHERE NOT EXISTS"
        " (SELECT 1 FROM ingredient_prices p WHERE p.ingredient_id = i.id)",
    )
    recents, anciens, obsoletes, derniere = (
        await _lignes(
            session,
            "SELECT count(*) FILTER (WHERE collected_at >= now() - make_interval(days => :recent)),"
            " count(*) FILTER (WHERE collected_at < now() - make_interval(days => :recent)"
            "   AND collected_at >= now() - make_interval(days => :ancien)),"
            " count(*) FILTER (WHERE collected_at < now() - make_interval(days => :ancien)),"
            " max(collected_at) FROM ingredient_prices",
            recent=SEUIL_RECENT_JOURS,
            ancien=SEUIL_ANCIEN_JOURS,
        )
    )[0]
    par_source = await _lignes(session, "SELECT source, count(*) FROM ingredient_prices GROUP BY 1")
    couverture = ratio((total or 0) - (sans_prix or 0), total)
    return [
        Indicateur(
            "prix.couverture",
            "Ingrédients avec au moins un prix",
            couverture,
            etat_couverture(couverture),
            f"{(total or 0) - (sans_prix or 0)}/{total} ingrédients",
            unite="ratio",
        ),
        Indicateur(
            "prix.fraicheur",
            "Observations par fraîcheur",
            recents + anciens + obsoletes,
            OK if recents + anciens + obsoletes else INCONNU,
            f"seuils {SEUIL_RECENT_JOURS} j / {SEUIL_ANCIEN_JOURS} j (services/pricing.py)",
            repartition=[
                {"libelle": "récent", "valeur": recents},
                {"libelle": "ancien", "valeur": anciens},
                {"libelle": "obsolète", "valeur": obsoletes},
            ],
        ),
        Indicateur(
            "prix.derniere",
            "Dernier relevé",
            derniere.isoformat() if derniere else None,
            OK if derniere else INCONNU,
            "max(ingredient_prices.collected_at)",
            repartition=[{"libelle": str(s), "valeur": n} for s, n in par_source],
        ),
    ]


def _mesure_collecte(depuis: datetime):
    async def mesurer(session: AsyncSession) -> list[Indicateur]:
        sources, actives = (
            await _lignes(
                session, "SELECT count(*), count(*) FILTER (WHERE is_active) FROM scraping_sources"
            )
        )[0]
        dernieres = await _lignes(
            session,
            "SELECT DISTINCT ON (subject) subject, status, created_at, alert FROM operation_runs"
            " WHERE kind LIKE 'scraping.%' ORDER BY subject, created_at DESC",
        )
        par_statut = await _lignes(
            session,
            "SELECT status, count(*) FROM operation_runs WHERE kind LIKE 'scraping.%'"
            " AND created_at >= :depuis GROUP BY 1",
            depuis=depuis,
        )
        a_revoir = await _scalaire(
            session,
            "SELECT count(DISTINCT label_normalized) FROM scraped_offers WHERE match_status = 'unmatched'",
        )
        en_erreur = sum(1 for _, statut, _, alerte in dernieres if str(statut) == "failed" or alerte)
        return [
            Indicateur(
                "collecte.sources",
                "Sources en erreur ou en alerte",
                en_erreur,
                etat_compte_problemes(en_erreur) if dernieres else INCONNU,
                f"dernière exécution de chacune des {sources} source(s), {actives} active(s)",
                repartition=[
                    {
                        "libelle": f"{sujet} · {statut}{' · alerte' if alerte else ''}",
                        "valeur": 1,
                        "date": cree.isoformat(),
                    }
                    for sujet, statut, cree, alerte in dernieres
                ],
            ),
            Indicateur(
                "collecte.executions",
                "Exécutions sur la période",
                sum(n for _, n in par_statut),
                OK,
                "operation_runs de type scraping.*",
                repartition=[{"libelle": str(s), "valeur": n} for s, n in par_statut],
            ),
            Indicateur(
                "collecte.revue",
                "Libellés en attente d'appariement",
                a_revoir,
                OK,
                "scraped_offers non appariées, libellés distincts",
                detail="Appariement exact uniquement : la file se vide par l'ajout d'alias",
            ),
        ]

    return mesurer


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------


async def mesurer(session: AsyncSession, *, periode_jours: int = 7) -> dict[str, Any]:
    maintenant = datetime.now(UTC)
    depuis = maintenant - timedelta(days=periode_jours)
    debut, fin = depuis.date(), maintenant.date()

    blocs = [
        Bloc("securite", "Sécurité alimentaire", "Doit rester à zéro : toute valeur non nulle est un incident."),
        Bloc("sante", "Santé du service", "Sondes réelles, interrogées à chaque affichage."),
        Bloc("catalogue", "Catalogue", "Plats, signatures d'allergènes et blocages de publication."),
        Bloc("generation", "Génération", "Programmes générés, échecs et rejets de la validation finale."),
        Bloc("cout", "Coût IA", "Appels, tokens et coût par jour."),
        Bloc("qualite", "Qualité des recommandations", "Acceptation, alternatives demandées et plats les plus proposés, suivis et remplacés."),
        Bloc("prix", "Prix", "Couverture et fraîcheur des observations."),
        Bloc("collecte", "Collecte", "Sources, exécutions et file de revue."),
    ]
    mesures = {
        "securite": _mesure_securite(depuis),
        "sante": _mesure_sante,
        "catalogue": _mesure_catalogue,
        "generation": _mesure_generation(depuis, debut, fin),
        "cout": _mesure_cout(depuis, debut, fin),
        "qualite": _mesure_qualite(depuis),
        "prix": _mesure_prix,
        "collecte": _mesure_collecte(depuis),
    }

    from app.core.database import check_database

    if not await check_database():
        motif = "base injoignable — vérifier DATABASE_* dans .env"
        for bloc in blocs:
            bloc.erreur = motif
            bloc.indicateurs = [
                Indicateur(f"{bloc.id}.indisponible", "Mesure indisponible", None, INCONNU, motif)
            ]
    else:
        for bloc in blocs:
            await _proteger(session, bloc, mesures[bloc.id])

    return {
        "genere_le": maintenant.isoformat(),
        "periode_jours": periode_jours,
        "base_joignable": all(b.erreur != "base injoignable — vérifier DATABASE_* dans .env" for b in blocs),
        "resume": resume(blocs),
        "blocs": [asdict(b) for b in blocs],
    }


def masquer_utilisateur(identifiant: str | None) -> str | None:
    """D-04 : l'identifiant est opaque, mais le tableau de bord n'en a pas besoin en entier."""
    if not identifiant:
        return None
    return identifiant[:6] + "…" if len(identifiant) > 6 else identifiant


__all__ = [
    "ALERTE",
    "Bloc",
    "CRITIQUE",
    "INCONNU",
    "Indicateur",
    "OK",
    "etat_compte_problemes",
    "etat_couverture",
    "etat_incidents",
    "etat_migration",
    "etat_non_verifies",
    "etat_taux_echec",
    "masquer_utilisateur",
    "mesurer",
    "motif_erreur",
    "ratio",
    "resume",
    "serie_journaliere",
    "tete_alembic",
]
