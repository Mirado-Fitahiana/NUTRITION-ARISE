"""Registre typé des réglages métier (plan §7 — FN-020, FN-024).

Avant ce registre, `planning._regles` lisait `app_settings` avec un repli
**silencieux** : une valeur mal saisie retombait sur le défaut sans que personne
ne le sache, et un jeu de poids écrit avec les noms du docstring de
`ScoringWeightSet` n'avait aucun effet. Le registre devient la source unique :

* il décrit chaque clé — type, bornes, défaut, référence de spec, **qui la lit** ;
* il valide à l'écriture (API de pilotage) *et* à la lecture (moteur) ;
* il rend, pour chaque clé, la valeur stockée, la valeur effective et le motif
  d'un éventuel rejet — c'est ce que la plateforme affiche.

Une clé « non branchée » est décrite, mais **refusée à l'écriture** : enregistrer
une valeur que rien ne lit laisserait croire à un réglage qui n'existe pas.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.configuration import AppSetting
from app.models.enums import MealSlot
from app.services.recommandation import (
    POIDS_PAR_DEFAUT,
    REPARTITION_PAR_DEFAUT,
    ReglesComposition,
)

logger = logging.getLogger("app.reglages")

ENTIER = "entier"
DECIMAL = "decimal"
BOOLEEN = "booleen"
REPARTITION = "repartition"

GROUPE_COMPOSITION = "Composition des programmes"
GROUPE_PRIX = "Prix"
GROUPE_IA = "Fournisseur IA"
GROUPE_COLLECTE = "Collecte"


class ReglageInvalide(ValueError):
    """Valeur refusée, avec un motif lisible par l'opérateur."""


@dataclass(frozen=True)
class Reglage:
    cle: str
    libelle: str
    groupe: str
    type: str
    defaut: Any
    description: str
    reference: str
    lu_par: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    unite: str | None = None
    #: Faux : décrit et affiché, mais refusé à l'écriture — rien ne le lit encore.
    branche: bool = True


REGISTRE: tuple[Reglage, ...] = (
    Reglage(
        cle="max_plan_days",
        libelle="Durée maximale d'un programme",
        groupe=GROUPE_COMPOSITION,
        type=ENTIER,
        defaut=7,
        description=(
            "Au-delà, la génération est refusée (PERIOD_INVALID). Les périodes de 14 "
            "et 21 jours se débloquent ici."
        ),
        reference="FN-006",
        lu_par="planning._regles → composer()",
        minimum=Decimal(1),
        maximum=Decimal(21),
        unite="jours",
    ),
    Reglage(
        cle="min_pool_size",
        libelle="Vivier minimal",
        groupe=GROUPE_COMPOSITION,
        type=ENTIER,
        defaut=25,
        description=(
            "En dessous de ce nombre de plats compatibles, le programme est refusé "
            "(CATALOG_TOO_SMALL) plutôt que dégradé."
        ),
        reference="FN-019 · §6.6",
        lu_par="planning._regles → composer()",
        minimum=Decimal(1),
        maximum=Decimal(500),
        unite="plats",
    ),
    Reglage(
        cle="max_repeats",
        libelle="Répétitions maximales d'un plat",
        groupe=GROUPE_COMPOSITION,
        type=ENTIER,
        defaut=2,
        description="Nombre de fois qu'un même plat peut revenir dans un programme.",
        reference="FN-024",
        lu_par="planning._regles → composer()",
        minimum=Decimal(1),
        maximum=Decimal(21),
        unite="fois",
    ),
    Reglage(
        cle="min_gap_days",
        libelle="Écart minimal entre deux services",
        groupe=GROUPE_COMPOSITION,
        type=ENTIER,
        defaut=2,
        description="Nombre de jours avant qu'un plat déjà servi puisse revenir.",
        reference="FN-024",
        lu_par="planning._regles → composer()",
        minimum=Decimal(0),
        maximum=Decimal(21),
        unite="jours",
    ),
    Reglage(
        cle="protein_rotation_window",
        libelle="Fenêtre de rotation des protéines",
        groupe=GROUPE_COMPOSITION,
        type=ENTIER,
        defaut=3,
        description=(
            "Jours pendant lesquels une même source de protéines est pénalisée "
            "(pénalité, jamais interdiction)."
        ),
        reference="FN-024",
        lu_par="planning._regles → composer()",
        minimum=Decimal(0),
        maximum=Decimal(21),
        unite="jours",
    ),
    Reglage(
        cle="daily_kcal_tolerance",
        libelle="Tolérance calorique journalière",
        groupe=GROUPE_COMPOSITION,
        type=DECIMAL,
        defaut=Decimal("0.10"),
        description=(
            "Écart relatif accepté entre les calories d'une journée et la cible "
            "(contrôle 5 de la validation finale)."
        ),
        reference="FN-022",
        lu_par="planning._regles → valider()",
        minimum=Decimal("0.01"),
        maximum=Decimal("0.50"),
        unite="ratio",
    ),
    Reglage(
        cle="meal_kcal_tolerance",
        libelle="Tolérance calorique par repas",
        groupe=GROUPE_COMPOSITION,
        type=DECIMAL,
        defaut=Decimal("0.20"),
        description=(
            "Transmise au moteur, mais aucun contrôle ne s'en sert encore : la "
            "modifier n'aurait aucun effet."
        ),
        reference="FN-023",
        lu_par="lue par planning._regles, utilisée par aucun contrôle",
        minimum=Decimal("0.01"),
        maximum=Decimal("0.80"),
        unite="ratio",
        branche=False,
    ),
    Reglage(
        cle="meal_distribution",
        libelle="Répartition des calories par créneau",
        groupe=GROUPE_COMPOSITION,
        type=REPARTITION,
        defaut=dict(REPARTITION_PAR_DEFAUT),
        description="Part de la cible calorique attribuée à chaque créneau. La somme vaut 1.",
        reference="FN-023",
        lu_par="planning._regles → composer()",
    ),
    Reglage(
        cle="price_recent_days",
        libelle="Seuil de fraîcheur « récent »",
        groupe=GROUPE_PRIX,
        type=ENTIER,
        defaut=14,
        description="Âge maximal d'un prix utilisable sans réserve.",
        reference="FN-016",
        lu_par="aucun — constante SEUIL_RECENT_JOURS de services/pricing.py",
        minimum=Decimal(1),
        maximum=Decimal(90),
        unite="jours",
        branche=False,
    ),
    Reglage(
        cle="price_stale_days",
        libelle="Seuil de fraîcheur « obsolète »",
        groupe=GROUPE_PRIX,
        type=ENTIER,
        defaut=45,
        description="Au-delà, un prix est exclu du chiffrage des budgets.",
        reference="FN-016",
        lu_par="aucun — constante SEUIL_ANCIEN_JOURS de services/pricing.py",
        minimum=Decimal(2),
        maximum=Decimal(365),
        unite="jours",
        branche=False,
    ),
    Reglage(
        cle="llm_monthly_budget",
        libelle="Budget LLM mensuel",
        groupe=GROUPE_IA,
        type=DECIMAL,
        defaut=None,
        description="Sans objet tant qu'aucun appel LLM n'est émis (décision du sprint 08).",
        reference="FN-021",
        lu_par="aucun",
        minimum=Decimal(0),
        maximum=Decimal(100000),
        unite="USD",
        branche=False,
    ),
    Reglage(
        cle="llm_user_quota",
        libelle="Quota LLM par utilisateur",
        groupe=GROUPE_IA,
        type=ENTIER,
        defaut=None,
        description="Sans objet tant qu'aucun appel LLM n'est émis (décision du sprint 08).",
        reference="FN-021",
        lu_par="aucun",
        minimum=Decimal(0),
        maximum=Decimal(1000),
        unite="générations",
        branche=False,
    ),
    Reglage(
        cle="scraping_write_enabled",
        libelle="Écriture des prix collectés",
        groupe=GROUPE_COLLECTE,
        type=BOOLEEN,
        defaut=False,
        description=(
            "Verrou n° 1 du plan : aucune offre collectée n'est promue en prix observé "
            "avant le feu vert du lot 4."
        ),
        reference="FN-013 · §6.5",
        lu_par="aucun — la promotion n'existe pas encore",
        branche=False,
    ),
    Reglage(
        cle="scraping_min_delay_s",
        libelle="Délai minimal entre deux pages",
        groupe=GROUPE_COLLECTE,
        type=DECIMAL,
        defaut=Decimal("3"),
        description=(
            "S'ajoute au délai de la source : le plus long s'applique. Le plancher de "
            "3 s est dans le code et ne se désactive pas."
        ),
        reference="FN-013",
        lu_par="collecte.moteur → delai_effectif()",
        minimum=Decimal(3),
        maximum=Decimal(60),
        unite="s",
    ),
    Reglage(
        cle="scraping_max_pages",
        libelle="Pages maximales par collecte",
        groupe=GROUPE_COLLECTE,
        type=ENTIER,
        defaut=10,
        description="Plafond global : le plus strict avec celui de la source s'applique.",
        reference="FN-013",
        lu_par="collecte.moteur → pages_effectives()",
        minimum=Decimal(1),
        maximum=Decimal(50),
        unite="pages",
    ),
    Reglage(
        cle="scraping_failure_alert_ratio",
        libelle="Seuil d'alerte de structure",
        groupe=GROUPE_COLLECTE,
        type=DECIMAL,
        defaut=Decimal("0.20"),
        description=(
            "Part de prix affichés illisibles au-delà de laquelle une alerte de "
            "changement de structure est levée."
        ),
        reference="FN-013",
        lu_par="collecte.moteur → _conclure()",
        minimum=Decimal("0.05"),
        maximum=Decimal("0.90"),
        unite="ratio",
    ),
)

PAR_CLE: dict[str, Reglage] = {r.cle: r for r in REGISTRE}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _decimal(brut: Any) -> Decimal:
    if isinstance(brut, bool):
        raise ReglageInvalide("booléen reçu, nombre attendu")
    try:
        valeur = Decimal(str(brut).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ReglageInvalide(f"« {brut} » n'est pas un nombre") from exc
    if not valeur.is_finite():
        raise ReglageInvalide(f"« {brut} » n'est pas un nombre fini")
    return valeur


def _bornes(reglage: Reglage, valeur: Decimal) -> None:
    if reglage.minimum is not None and valeur < reglage.minimum:
        raise ReglageInvalide(f"{valeur} est sous le minimum ({reglage.minimum})")
    if reglage.maximum is not None and valeur > reglage.maximum:
        raise ReglageInvalide(f"{valeur} dépasse le maximum ({reglage.maximum})")


def valider(reglage: Reglage, brut: Any) -> Any:
    """Valeur normalisée, ou `ReglageInvalide` avec son motif."""
    if brut is None:
        raise ReglageInvalide("valeur absente")

    if reglage.type == BOOLEEN:
        if isinstance(brut, bool):
            return brut
        raise ReglageInvalide("booléen attendu (true ou false)")

    if reglage.type == ENTIER:
        valeur = _decimal(brut)
        if valeur != valeur.to_integral_value():
            raise ReglageInvalide(f"{valeur} n'est pas un entier")
        _bornes(reglage, valeur)
        return int(valeur)

    if reglage.type == DECIMAL:
        valeur = _decimal(brut)
        _bornes(reglage, valeur)
        return valeur

    if reglage.type == REPARTITION:
        if not isinstance(brut, dict) or not brut:
            raise ReglageInvalide("objet { créneau: part } attendu")
        parts: dict[MealSlot, Decimal] = {}
        for cle, part in brut.items():
            try:
                creneau = MealSlot(cle)
            except ValueError as exc:
                raise ReglageInvalide(f"créneau inconnu : {cle}") from exc
            valeur = _decimal(part)
            if not Decimal(0) <= valeur <= Decimal(1):
                raise ReglageInvalide(f"part hors de [0, 1] pour {cle}")
            parts[creneau] = valeur
        somme = sum(parts.values(), Decimal(0))
        if abs(somme - Decimal(1)) > Decimal("0.001"):
            raise ReglageInvalide(f"la somme des parts vaut {somme}, elle doit valoir 1")
        return parts

    raise ReglageInvalide(f"type de réglage inconnu : {reglage.type}")


def affichage(valeur: Any) -> Any:
    """Forme JSON d'une valeur : décimaux en texte, créneaux en clés texte."""
    if isinstance(valeur, Decimal):
        return str(valeur)
    if isinstance(valeur, dict):
        return {str(k): affichage(v) for k, v in valeur.items()}
    return valeur


def en_json(reglage: Reglage, valeur: Any) -> dict[str, Any]:
    """Forme stockée dans `app_settings.value`. L'enveloppe `value` est celle que
    `planning._regles` lisait déjà."""
    return {"value": affichage(valeur)}


def extraire(stocke: Any) -> Any:
    if isinstance(stocke, dict) and "value" in stocke:
        return stocke["value"]
    return stocke


# --------------------------------------------------------------------------
# Résolution
# --------------------------------------------------------------------------


@dataclass
class EtatReglage:
    reglage: Reglage
    present: bool
    stocke: Any
    effectif: Any
    #: `base` (valeur stockée valide), `defaut` (rien de stocké), `rejete`
    #: (valeur stockée refusée : le défaut s'applique, et la page le dit).
    source: str
    motif: str | None = None
    mis_a_jour_le: datetime | None = None
    mis_a_jour_par: str | None = None


def resoudre(lignes: dict[str, dict[str, Any]]) -> dict[str, EtatReglage]:
    """`lignes` : clé → {"value", "updated_at", "updated_by"} tels que stockés."""
    etats: dict[str, EtatReglage] = {}
    for reglage in REGISTRE:
        ligne = lignes.get(reglage.cle)
        if ligne is None:
            etats[reglage.cle] = EtatReglage(reglage, False, None, reglage.defaut, "defaut")
            continue
        brut = extraire(ligne.get("value"))
        commun = {
            "mis_a_jour_le": ligne.get("updated_at"),
            "mis_a_jour_par": ligne.get("updated_by"),
        }
        try:
            etats[reglage.cle] = EtatReglage(
                reglage, True, brut, valider(reglage, brut), "base", **commun
            )
        except ReglageInvalide as exc:
            logger.warning("réglage %s rejeté à la lecture : %s", reglage.cle, exc)
            etats[reglage.cle] = EtatReglage(
                reglage, True, brut, reglage.defaut, "rejete", str(exc), **commun
            )

    # FN-016 — les deux seuils de fraîcheur n'ont de sens qu'ordonnés.
    recent, ancien = etats["price_recent_days"], etats["price_stale_days"]
    if recent.effectif >= ancien.effectif:
        for etat in (recent, ancien):
            if etat.source == "base":
                etat.source = "rejete"
                etat.effectif = etat.reglage.defaut
                etat.motif = "le seuil « récent » doit rester inférieur au seuil « obsolète »"
    return etats


def cles_inconnues(lignes: dict[str, Any]) -> list[str]:
    """Clés présentes en base que le registre ne connaît pas : sans effet."""
    return sorted(set(lignes) - set(PAR_CLE))


def regles_depuis(
    etats: dict[str, EtatReglage], surcharges: dict[str, Any] | None = None
) -> ReglesComposition:
    """Règles de composition effectives. `surcharges` (laboratoire) passe par la
    même validation que l'API."""
    valeurs = {cle: etat.effectif for cle, etat in etats.items()}
    for cle, brut in (surcharges or {}).items():
        reglage = PAR_CLE.get(cle)
        if reglage is None:
            raise ReglageInvalide(f"réglage inconnu : {cle}")
        valeurs[cle] = valider(reglage, brut)
    return ReglesComposition(
        max_plan_days=valeurs["max_plan_days"],
        min_pool_size=valeurs["min_pool_size"],
        max_repeats=valeurs["max_repeats"],
        min_gap_days=valeurs["min_gap_days"],
        protein_rotation_window=valeurs["protein_rotation_window"],
        daily_kcal_tolerance=valeurs["daily_kcal_tolerance"],
        meal_kcal_tolerance=valeurs["meal_kcal_tolerance"],
        meal_distribution=dict(valeurs["meal_distribution"]),
    )


def regles_par_defaut() -> ReglesComposition:
    return regles_depuis(resoudre({}))


def valeurs_collecte(etats: dict[str, EtatReglage]) -> dict[str, Any]:
    return {
        cle: etats[cle].effectif
        for cle in ("scraping_min_delay_s", "scraping_max_pages", "scraping_failure_alert_ratio")
    }


def vue_etat(etat: EtatReglage) -> dict[str, Any]:
    r = etat.reglage
    return {
        "cle": r.cle,
        "libelle": r.libelle,
        "groupe": r.groupe,
        "type": r.type,
        "description": r.description,
        "reference": r.reference,
        "lu_par": r.lu_par,
        "unite": r.unite,
        "minimum": affichage(r.minimum),
        "maximum": affichage(r.maximum),
        "branche": r.branche,
        "defaut": affichage(r.defaut),
        "present": etat.present,
        "stocke": affichage(etat.stocke),
        "effectif": affichage(etat.effectif),
        "source": etat.source,
        "motif": etat.motif,
        "mis_a_jour_le": etat.mis_a_jour_le.isoformat() if etat.mis_a_jour_le else None,
        "mis_a_jour_par": etat.mis_a_jour_par,
    }


async def lire_lignes(session: AsyncSession) -> dict[str, dict[str, Any]]:
    return {
        row.key: {"value": row.value, "updated_at": row.updated_at, "updated_by": row.updated_by}
        for row in await session.scalars(select(AppSetting))
    }


async def lire_etats(session: AsyncSession) -> dict[str, EtatReglage]:
    return resoudre(await lire_lignes(session))


# --------------------------------------------------------------------------
# Poids du score (FN-020)
# --------------------------------------------------------------------------

#: Les seules clés que `recommandation.scorer` lit.
POIDS_AUTORISES: tuple[str, ...] = tuple(POIDS_PAR_DEFAUT)

POIDS_LIBELLES: dict[str, str] = {
    "nutrition": "Adéquation calorique",
    "cost": "Coût",
    "preference": "Ingrédients préférés",
    "variety": "Variété",
    "favorite": "Ingrédient favori",
}

#: Termes de la formule FN-020 que `scorer` ne calcule pas encore.
POIDS_PREVUS: dict[str, str] = {
    "w_local": "produits locaux",
    "w_facilite": "facilité de préparation",
    "w_reemploi": "réemploi d'ingrédients déjà achetés",
    "w_historique": "historique d'acceptation",
    "p_repetition": "pénalité de répétition",
    "p_refus": "pénalité de refus",
    "p_prix_ancien": "pénalité de prix ancien (lot 3)",
    "w_semantique": "similarité sémantique (lot 4)",
}

POIDS_MAX = Decimal(5)
MOTIF_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


def valider_version(version: str) -> str:
    propre = (version or "").strip()
    if not MOTIF_VERSION.match(propre):
        raise ReglageInvalide(
            "version : minuscules, chiffres, « . », « _ » ou « - », 32 caractères au plus"
        )
    if propre == "fallback-1":
        raise ReglageInvalide("« fallback-1 » est réservée aux générations sans jeu actif")
    return propre


def valider_poids(poids: dict[str, Any]) -> dict[str, Decimal]:
    """Un jeu complet, borné, et limité aux clés réellement lues."""
    if not isinstance(poids, dict):
        raise ReglageInvalide("objet { terme: poids } attendu")

    inconnues = sorted(set(poids) - set(POIDS_AUTORISES))
    if inconnues:
        prevues = [c for c in inconnues if c in POIDS_PREVUS]
        motif = f"clés refusées : {', '.join(inconnues)}"
        if prevues:
            motif += (
                f" — {', '.join(prevues)} figure(nt) dans FN-020 mais `scorer` ne les "
                "calcule pas : les enregistrer n'aurait aucun effet"
            )
        raise ReglageInvalide(motif)

    manquantes = sorted(set(POIDS_AUTORISES) - set(poids))
    if manquantes:
        raise ReglageInvalide(f"poids manquants : {', '.join(manquantes)}")

    resultat: dict[str, Decimal] = {}
    for cle in POIDS_AUTORISES:
        valeur = _decimal(poids[cle])
        if not Decimal(0) <= valeur <= POIDS_MAX:
            raise ReglageInvalide(f"{cle} : {valeur} hors de [0, {POIDS_MAX}]")
        resultat[cle] = valeur.quantize(Decimal("0.0001"))
    if sum(resultat.values(), Decimal(0)) == 0:
        raise ReglageInvalide("au moins un poids doit être non nul")
    return resultat


def poids_depuis(stockes: dict[str, Any] | None) -> tuple[dict[str, Decimal], list[str]]:
    """Poids effectifs d'un jeu stocké, et les clés ignorées (inconnues ou invalides)."""
    poids = dict(POIDS_PAR_DEFAUT)
    ignores: list[str] = []
    for cle, valeur in (stockes or {}).items():
        if cle not in POIDS_AUTORISES:
            ignores.append(cle)
            continue
        try:
            nombre = _decimal(valeur)
        except ReglageInvalide:
            ignores.append(cle)
            continue
        if not Decimal(0) <= nombre <= POIDS_MAX:
            ignores.append(cle)
            continue
        poids[cle] = nombre
    if ignores:
        logger.warning("poids ignorés à la lecture : %s", ", ".join(sorted(ignores)))
    return poids, sorted(ignores)


__all__ = [
    "BOOLEEN",
    "DECIMAL",
    "ENTIER",
    "EtatReglage",
    "PAR_CLE",
    "POIDS_AUTORISES",
    "POIDS_LIBELLES",
    "POIDS_MAX",
    "POIDS_PREVUS",
    "REGISTRE",
    "REPARTITION",
    "Reglage",
    "ReglageInvalide",
    "affichage",
    "cles_inconnues",
    "en_json",
    "extraire",
    "lire_etats",
    "lire_lignes",
    "poids_depuis",
    "regles_depuis",
    "regles_par_defaut",
    "resoudre",
    "valeurs_collecte",
    "valider",
    "valider_poids",
    "valider_version",
    "vue_etat",
]
