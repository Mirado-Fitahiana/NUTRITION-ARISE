"""Laboratoire de recommandation — le « RAG » d'ARISE, visible et testable (plan §8).

Le RAG classique, où le modèle choisit parmi des documents récupérés, est
**interdit** par D-06. Dans ARISE, les rôles sont redistribués, et ce module
montre chacun d'eux :

| Étape         | Dans ARISE                                            | Qui décide |
|---------------|-------------------------------------------------------|------------|
| Récupération  | filtrage dur (FN-019), puis score métier (FN-020)     | le code    |
| Augmentation  | contexte = plats **déjà retenus** et motifs (FN-021)  | le code    |
| Génération    | **un** appel qui rédige la justification (D-07)       | le LLM     |
| Contrôle      | validation rejouée après le LLM (FN-022)              | le code    |

Trois garanties, vérifiées par `tests/test_laboratoire.py` :

* **rien n'est écrit** — ni programme, ni run : le moteur est appelé tel quel,
  sur des plats déjà lus ;
* **le contexte envoyé au modèle ne contient aucune donnée interdite** par
  FN-021, même quand le profil d'essai en porte (date de naissance, poids) ;
* **une réponse de modèle qui cite un plat inconnu, ou écrit un chiffre, est
  rejetée** et le texte de repli s'applique.

Aucun appel LLM réel n'est émis en v1 (décision du sprint 08). Le laboratoire
éprouve la validation sur une **réponse simulée**, collée à l'écran.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from app.models.enums import ActivityLevel, Allergen, Goal, MealSlot, RestrictionType, Sex
from app.services.energie import calculer
from app.services.recommandation import (
    CONTROLES_SECURITE,
    SLOTS_PAR_TYPE,
    ContraintesUtilisateur,
    EchecGeneration,
    PlatCandidat,
    ProgrammeGenere,
    ReglesComposition,
    ResultatValidation,
    ScoreDetaille,
    TraceCreneau,
    composer,
    filtrer,
    justification_de_repli,
    valider,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_FICTIF = ROOT / "evaluation" / "catalogues" / "fictif.yaml"

ORDRE_CRENEAUX: tuple[str, ...] = tuple(s.value for s in MealSlot)
LIBELLES_CRENEAUX: dict[str, str] = {
    "breakfast": "Petit-déjeuner",
    "morning_snack": "Collation du matin",
    "lunch": "Déjeuner",
    "afternoon_snack": "Collation de l'après-midi",
    "dinner": "Dîner",
}

#: Contrôles de `valider()`. FN-022 en prévoit onze ; seuls ceux-ci existent.
LIBELLES_CONTROLES: dict[int, str] = {
    1: "Aucun allergène déclaré",
    2: "Restrictions respectées",
    3: "Aucun ingrédient refusé",
    4: "Tout plat provient du catalogue",
    5: "Calories du jour dans la tolérance",
}

MOTIF_LLM_DESACTIVE = (
    "Aucun appel LLM en v1 (décision du sprint 08). Un essai réel attend le "
    "renouvellement de la clé OpenRouter (plan, décision 8). Collez une réponse "
    "simulée pour éprouver la validation."
)

MOTIFS_SELECTION: dict[str, str] = {
    "nutrition": "proche de la cible calorique du créneau",
    "cost": "coût maîtrisé",
    "preference": "contient des ingrédients préférés",
    "variety": "apporte de la variété",
    "favorite": "contient un ingrédient favori",
}

COMPOSANTES_SCORE: tuple[str, ...] = (
    "nutrition",
    "cost",
    "preference",
    "variety",
    "favorite",
)


class CatalogueInvalide(ValueError):
    pass


class ProfilInvalide(ValueError):
    pass


def _f(valeur: Decimal | float | int | None, chiffres: int = 2) -> float | None:
    if valeur is None:
        return None
    return round(float(valeur), chiffres)


def _jsonable(valeur: Any) -> Any:
    return json.loads(json.dumps(valeur, default=str))


# --------------------------------------------------------------------------
# Catalogue fictif
# --------------------------------------------------------------------------


def charger_catalogue_fictif(chemin: Path = CATALOGUE_FICTIF) -> tuple[list[PlatCandidat], dict[str, Any]]:
    """Un fichier qui ne se déclare pas `fictif: true` est refusé : ce catalogue
    ne doit jamais pouvoir passer pour le vrai."""
    try:
        donnees = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogueInvalide(f"catalogue fictif illisible : {exc}") from exc
    if not isinstance(donnees, dict) or donnees.get("fictif") is not True:
        raise CatalogueInvalide("le fichier ne se déclare pas « fictif: true » : refusé")

    types_connus = set(SLOTS_PAR_TYPE)
    plats: list[PlatCandidat] = []
    vus: set[str] = set()
    for numero, entree in enumerate(donnees.get("plats") or [], start=1):
        try:
            slug = str(entree["slug"])
            if slug in vus:
                raise CatalogueInvalide(f"slug en double : {slug}")
            vus.add(slug)
            types = frozenset(str(t) for t in entree["types"])
            if not types or not types <= types_connus:
                raise CatalogueInvalide(f"types de repas inconnus : {sorted(types - types_connus)}")
            plats.append(
                PlatCandidat(
                    slug=slug,
                    name=str(entree["nom"]),
                    meal_types=types,
                    kcal=Decimal(str(entree["kcal"])),
                    protein_g=Decimal(str(entree.get("proteines_g", 0))),
                    carbs_g=Decimal(str(entree.get("glucides_g", 0))),
                    fat_g=Decimal(str(entree.get("lipides_g", 0))),
                    allergens=frozenset(Allergen(a) for a in entree.get("allergenes") or []),
                    compatible_restrictions=frozenset(
                        RestrictionType(r) for r in entree.get("restrictions") or []
                    ),
                    compatible_goals=frozenset(Goal(g) for g in entree.get("objectifs") or []),
                    ingredient_slugs=frozenset(str(i) for i in entree.get("ingredients") or []),
                    protein_sources=frozenset(str(p) for p in entree.get("proteines") or []),
                    estimated_cost=(
                        Decimal(str(entree["cout"])) if entree.get("cout") is not None else None
                    ),
                )
            )
        except CatalogueInvalide as exc:
            raise CatalogueInvalide(f"plat n° {numero} : {exc}") from exc
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            raise CatalogueInvalide(f"plat n° {numero} : {type(exc).__name__} {exc}") from exc

    return plats, {
        "fictif": True,
        "titre": donnees.get("titre") or "Catalogue fictif",
        "avertissement": donnees.get("avertissement"),
        "total": len(plats),
    }


def ingredients_du_catalogue(plats: list[PlatCandidat]) -> list[str]:
    return sorted({i for p in plats for i in p.ingredient_slugs})


# --------------------------------------------------------------------------
# Profil d'essai
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfilEssai:
    contraintes: ContraintesUtilisateur
    cible: dict[str, Any]
    #: Valeurs de profil qui ne doivent jamais atteindre le modèle (FN-021).
    valeurs_sensibles: tuple[str, ...] = ()


def _ensemble(valeurs: Any, conversion) -> frozenset:
    try:
        return frozenset(conversion(v) for v in (valeurs or []))
    except ValueError as exc:
        raise ProfilInvalide(str(exc)) from exc


def profil_depuis(donnees: dict[str, Any]) -> ProfilEssai:
    """Profil d'essai → contraintes du moteur. La cible calorique est saisie, ou
    calculée par `energie.calculer` (planchers de sécurité compris)."""
    try:
        objectif = Goal(donnees.get("goal") or Goal.BALANCED_DIET)
    except ValueError as exc:
        raise ProfilInvalide(f"objectif inconnu : {donnees.get('goal')}") from exc

    physio = donnees.get("physio")
    sensibles: tuple[str, ...] = ()
    if donnees.get("kcal_target") not in (None, ""):
        try:
            kcal = Decimal(str(donnees["kcal_target"]))
        except InvalidOperation as exc:
            raise ProfilInvalide("cible calorique illisible") from exc
        if not Decimal(800) <= kcal <= Decimal(6000):
            raise ProfilInvalide("cible calorique hors de [800, 6000] kcal")
        cible: dict[str, Any] = {"kcal": _f(kcal, 0), "origine": "saisie"}
    elif physio:
        try:
            naissance = date.fromisoformat(str(physio["birth_date"]))
            besoins = calculer(
                poids_kg=Decimal(str(physio["weight_kg"])),
                taille_cm=Decimal(str(physio["height_cm"])),
                birth_date=naissance,
                sexe=Sex(physio.get("sex") or Sex.UNSPECIFIED),
                niveau_activite=ActivityLevel(physio.get("activity_level") or ActivityLevel.SEDENTARY),
                objectif=objectif,
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise ProfilInvalide(f"profil physiologique invalide : {exc}") from exc
        kcal = besoins.kcal_target
        cible = {
            "kcal": _f(kcal, 0),
            "origine": "calculee",
            "plancher_applique": besoins.safety_floor_applied,
            "besoins": {
                "bmr": _f(besoins.bmr, 0),
                "tdee": _f(besoins.tdee, 0),
                "proteines_g": _f(besoins.protein_g, 0),
                "glucides_g": _f(besoins.carbs_g, 0),
                "lipides_g": _f(besoins.fat_g, 0),
                "formule": f"{besoins.formula} {besoins.formula_version}",
            },
        }
        sensibles = tuple(
            str(physio[c]) for c in ("birth_date", "weight_kg", "height_cm") if physio.get(c)
        )
    else:
        raise ProfilInvalide("renseigner une cible calorique ou un profil physiologique")

    budget = donnees.get("budget_par_jour")
    return ProfilEssai(
        contraintes=ContraintesUtilisateur(
            goal=objectif,
            kcal_target=kcal,
            allergens=_ensemble(donnees.get("allergens"), Allergen),
            restrictions=_ensemble(donnees.get("restrictions"), RestrictionType),
            disliked_ingredients=_ensemble(donnees.get("disliked_ingredients"), str),
            preferred_ingredients=_ensemble(donnees.get("preferred_ingredients"), str),
            favorite_ingredients=_ensemble(donnees.get("favorite_ingredients"), str),
            budget_par_jour=Decimal(str(budget)) if budget not in (None, "") else None,
        ),
        cible=cible,
        valeurs_sensibles=sensibles,
    )


# --------------------------------------------------------------------------
# Contexte transmis au modèle (FN-021)
# --------------------------------------------------------------------------

#: FN-021 — données interdites : identité, email, jeton, mot de passe,
#: localisation précise, historique médical, et tout ce qui n'est pas
#: nécessaire à la rédaction.
CLES_INTERDITES_LLM = frozenset(
    {
        "external_user_id", "user_id", "profile_id", "sub", "email", "phone",
        "token", "access_token", "refresh_token", "authorization", "password",
        "secret", "api_key", "latitude", "longitude", "city", "district", "address",
        "birth_date", "age", "weight_kg", "height_cm", "bmi", "physio",
        "declared_pregnancy", "declared_breastfeeding", "declared_medical_condition",
        "medical_history",
    }
)

SCHEMA_REPONSE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan_summary", "days"],
    "properties": {
        "plan_summary": {"type": "string"},
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["day", "meals"],
                "properties": {
                    "day": {"type": "integer"},
                    "meals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["meal_id", "justification"],
                            "properties": {
                                "meal_id": {"type": "string"},
                                "justification": {"type": "string"},
                                "nutrition_note": {"type": "string"},
                            },
                        },
                    },
                    "tip": {"type": "string"},
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


def meal_id(jour: int, slot: MealSlot | str) -> str:
    return f"J{jour + 1}-{slot.value if isinstance(slot, MealSlot) else slot}"


def _motifs(score: ScoreDetaille) -> list[str]:
    contributions = sorted(
        ((valeur, cle) for cle, valeur in score.detail.items() if cle in MOTIFS_SELECTION and valeur > 0),
        reverse=True,
    )
    return [MOTIFS_SELECTION[cle] for _, cle in contributions[:2]]


def construire_contexte(programme: ProgrammeGenere, contraintes: ContraintesUtilisateur) -> dict[str, Any]:
    """Ce qui serait envoyé au modèle : un programme **déjà décidé**."""
    return {
        "version_prompt": "labo-1",
        "objectif": contraintes.goal.value,
        "cible_kcal_jour": int(contraintes.kcal_target),
        "contraintes_actives": {
            "allergenes": sorted(a.value for a in contraintes.allergens),
            "restrictions": sorted(r.value for r in contraintes.restrictions),
        },
        "jours": [
            {
                "jour": journee.index + 1,
                "repas": [
                    {
                        "meal_id": meal_id(journee.index, repas.slot),
                        "creneau": LIBELLES_CRENEAUX[repas.slot.value],
                        "plat": repas.plat.name,
                        "motifs_selection": _motifs(repas.score),
                        "cout_estime_mga": (
                            int(repas.plat.estimated_cost)
                            if repas.plat.estimated_cost is not None
                            else None
                        ),
                    }
                    for repas in sorted(journee.repas, key=lambda r: ORDRE_CRENEAUX.index(r.slot.value))
                ],
            }
            for journee in programme.journees
        ],
        "consignes": [
            "Rédige la présentation d'un programme déjà décidé : tu ne choisis, n'ajoutes ni ne remplaces aucun plat.",
            "N'utilise que les meal_id fournis.",
            "N'écris aucun chiffre : calories, quantités et prix sont ajoutés par le code.",
            "Réponds uniquement par un objet JSON conforme au schéma.",
        ],
        "schema_reponse": SCHEMA_REPONSE,
    }


def verifier_contexte(payload: Any, valeurs_sensibles: tuple[str, ...] = ()) -> list[str]:
    """Violations de FN-021 : clés interdites, ou valeurs de profil recopiées."""
    violations: list[str] = []

    def parcourir(valeur: Any, chemin: str) -> None:
        if isinstance(valeur, dict):
            for cle, sous in valeur.items():
                ici = f"{chemin}.{cle}" if chemin else str(cle)
                if str(cle).lower() in CLES_INTERDITES_LLM:
                    violations.append(f"clé interdite « {cle} » ({ici})")
                parcourir(sous, ici)
        elif isinstance(valeur, (list, tuple)):
            for i, sous in enumerate(valeur):
                parcourir(sous, f"{chemin}[{i}]")
        elif isinstance(valeur, (str, int, float)) and not isinstance(valeur, bool):
            texte = str(valeur)
            for sensible in valeurs_sensibles:
                if sensible and (texte == sensible or (len(sensible) >= 6 and sensible in texte)):
                    violations.append(f"valeur de profil transmise ({chemin})")

    parcourir(payload, "")
    return violations


_CHIFFRE = re.compile(r"\d")
CLES_REPONSE = frozenset({"plan_summary", "days", "warnings"})
CLES_JOUR = frozenset({"day", "meals", "tip"})
CLES_REPAS = frozenset({"meal_id", "justification", "nutrition_note"})


def verifier_reponse_llm(reponse: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """FN-021 — schéma strict, identifiants connus, aucun chiffre dans le texte."""
    attendus = {r["meal_id"] for j in payload["jours"] for r in j["repas"]}
    erreurs: list[str] = []
    avertissements: list[str] = []
    inconnus: list[str] = []
    vus: set[str] = set()
    avec_chiffres: list[str] = []

    def texte(valeur: Any, ou: str) -> None:
        if valeur is None:
            return
        if not isinstance(valeur, str):
            erreurs.append(f"{ou} : texte attendu")
            return
        if _CHIFFRE.search(valeur):
            avec_chiffres.append(valeur[:80])

    if not isinstance(reponse, dict):
        return {
            "valide": False,
            "erreurs": ["la réponse n'est pas un objet JSON"],
            "avertissements": [],
            "meal_ids_inconnus": [],
            "extraits_avec_chiffres": [],
            "meal_ids_couverts": 0,
            "meal_ids_attendus": len(attendus),
        }

    for cle in sorted(set(reponse) - CLES_REPONSE):
        erreurs.append(f"champ inattendu : {cle} (schéma strict)")
    if not isinstance(reponse.get("plan_summary"), str):
        erreurs.append("plan_summary manquant ou non textuel")
    texte(reponse.get("plan_summary"), "plan_summary")

    jours = reponse.get("days")
    if not isinstance(jours, list):
        erreurs.append("days manquant ou non tableau")
        jours = []
    for i, jour in enumerate(jours):
        if not isinstance(jour, dict):
            erreurs.append(f"days[{i}] : objet attendu")
            continue
        for cle in sorted(set(jour) - CLES_JOUR):
            erreurs.append(f"days[{i}] : champ inattendu {cle}")
        texte(jour.get("tip"), f"days[{i}].tip")
        repas_liste = jour.get("meals")
        if not isinstance(repas_liste, list):
            erreurs.append(f"days[{i}].meals : tableau attendu")
            continue
        for k, repas in enumerate(repas_liste):
            if not isinstance(repas, dict):
                erreurs.append(f"days[{i}].meals[{k}] : objet attendu")
                continue
            for cle in sorted(set(repas) - CLES_REPAS):
                erreurs.append(f"days[{i}].meals[{k}] : champ inattendu {cle}")
            identifiant = repas.get("meal_id")
            if identifiant in attendus:
                vus.add(identifiant)
            else:
                inconnus.append(str(identifiant))
            if not isinstance(repas.get("justification"), str):
                erreurs.append(f"days[{i}].meals[{k}] : justification manquante")
            texte(repas.get("justification"), "justification")
            texte(repas.get("nutrition_note"), "nutrition_note")

    for n, avertissement in enumerate(reponse.get("warnings") or []):
        texte(avertissement, f"warnings[{n}]")

    if inconnus:
        erreurs.append(
            f"identifiant inconnu : {', '.join(inconnus)} — le modèle ne peut citer que les meal_id fournis"
        )
    if avec_chiffres:
        erreurs.append(
            f"chiffre dans le texte ({len(avec_chiffres)} extrait(s)) — le modèle ne produit aucun chiffre"
        )
    manquants = attendus - vus
    if manquants and not inconnus:
        avertissements.append(
            f"justification absente pour {len(manquants)} repas : texte de repli pour ceux-là"
        )

    return {
        "valide": not erreurs,
        "erreurs": erreurs,
        "avertissements": avertissements,
        "meal_ids_inconnus": inconnus,
        "extraits_avec_chiffres": avec_chiffres[:5],
        "meal_ids_couverts": len(vus),
        "meal_ids_attendus": len(attendus),
    }


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


def vue_regles(regles: ReglesComposition) -> dict[str, Any]:
    return {
        "max_plan_days": regles.max_plan_days,
        "min_pool_size": regles.min_pool_size,
        "max_repeats": regles.max_repeats,
        "min_gap_days": regles.min_gap_days,
        "protein_rotation_window": regles.protein_rotation_window,
        "daily_kcal_tolerance": str(regles.daily_kcal_tolerance),
        "meal_kcal_tolerance": str(regles.meal_kcal_tolerance),
        "meal_distribution": {str(k): str(v) for k, v in regles.meal_distribution.items()},
    }


def vue_validation(resultat: ResultatValidation) -> dict[str, Any]:
    return {
        "conforme": resultat.conforme,
        "incident_securite": resultat.incident_securite,
        "controles": [
            {
                "numero": numero,
                "libelle": libelle,
                "statut": "echec" if numero in resultat.controles_en_echec else "ok",
                "securite": numero in CONTROLES_SECURITE,
            }
            for numero, libelle in sorted(LIBELLES_CONTROLES.items())
        ],
        "non_implementes": "Les contrôles 6 à 11 de FN-022 ne sont pas encore implémentés dans valider().",
        "details": _jsonable(resultat.details),
    }


def _vue_creneau(trace: TraceCreneau, par_slug: dict[str, PlatCandidat], limite: int = 8) -> dict[str, Any]:
    return {
        "jour": trace.jour,
        "slot": trace.slot.value,
        "creneau": LIBELLES_CRENEAUX[trace.slot.value],
        "kcal_cible": _f(trace.kcal_cible, 0),
        "nb_candidats": len(trace.candidats),
        "candidats": [
            {
                "rang": rang,
                "slug": plat.slug,
                "nom": plat.name,
                "kcal": _f(plat.kcal, 0),
                "total": _f(score.total, 4),
                "detail": {cle: _f(valeur, 4) for cle, valeur in score.detail.items()},
                "retenu": rang == 1,
            }
            for rang, (score, plat) in enumerate(trace.candidats[:limite], start=1)
        ],
        "ecartes": [
            {"slug": slug, "nom": par_slug[slug].name if slug in par_slug else slug, "motif": motif}
            for slug, motif in trace.ecartes
        ],
    }


def simuler(
    plats: list[PlatCandidat],
    profil: ProfilEssai,
    *,
    jours: int,
    regles: ReglesComposition,
    poids: dict[str, Decimal],
    seed: int,
    reponse_llm: Any = None,
) -> dict[str, Any]:
    """Toutes les étapes, sans rien écrire. Déterministe à graine égale."""
    debut = time.perf_counter()
    contraintes = profil.contraintes
    par_slug = {p.slug: p for p in plats}

    vivier = filtrer(list(plats), contraintes)
    motifs = Counter(e.motif for e in vivier.exclus)
    resultat: dict[str, Any] = {
        "vivier": {
            "total": len(plats),
            "retenus": vivier.taille,
            "min_pool_size": regles.min_pool_size,
            "exclus": [
                {"slug": e.slug, "nom": par_slug[e.slug].name, "motif": e.motif} for e in vivier.exclus
            ],
            "par_motif": [{"libelle": m, "valeur": n} for m, n in motifs.most_common()],
        }
    }

    trace: list[TraceCreneau] = []
    programme = composer(vivier, contraintes, jours, regles, poids=poids, seed=seed, trace=trace)
    resultat["score"] = {
        "composantes": list(COMPOSANTES_SCORE),
        "creneaux": [_vue_creneau(t, par_slug) for t in trace],
    }

    if isinstance(programme, EchecGeneration):
        resultat.update(
            composition={
                "statut": "echec",
                "seed": seed,
                "echec": {
                    "code": programme.code,
                    "message": programme.message,
                    "details": _jsonable(programme.details),
                },
            },
            validation=None,
            contexte=None,
            redaction=None,
            apres_llm=None,
        )
        resultat["duree_ms"] = round((time.perf_counter() - debut) * 1000, 2)
        return resultat

    cible = contraintes.kcal_target
    resultat["composition"] = {
        "statut": "ok",
        "seed": programme.seed,
        "vivier_taille": programme.vivier_taille,
        "tolerance_jour": _f(regles.daily_kcal_tolerance, 3),
        "non_couverts": [
            {"jour": j, "slot": s.value, "creneau": LIBELLES_CRENEAUX[s.value]}
            for j, s in programme.creneaux_non_couverts
        ],
        "jours": [
            {
                "index": journee.index,
                "kcal_total": _f(journee.kcal_total, 0),
                "kcal_cible": _f(cible, 0),
                "ecart": _f((journee.kcal_total - cible) / cible, 3) if cible else None,
                "dans_tolerance": (
                    bool(cible) and abs(journee.kcal_total - cible) / cible <= regles.daily_kcal_tolerance
                ),
                "repas": [
                    {
                        "meal_id": meal_id(journee.index, repas.slot),
                        "slot": repas.slot.value,
                        "creneau": LIBELLES_CRENEAUX[repas.slot.value],
                        "slug": repas.plat.slug,
                        "nom": repas.plat.name,
                        "kcal": _f(repas.plat.kcal, 0),
                        "kcal_cible": _f(repas.kcal_cible, 0),
                        "score": _f(repas.score.total, 4),
                        "cout": _f(repas.plat.estimated_cost, 0),
                        "allergenes": sorted(a.value for a in repas.plat.allergens),
                    }
                    for repas in sorted(journee.repas, key=lambda r: ORDRE_CRENEAUX.index(r.slot.value))
                ],
            }
            for journee in programme.journees
        ],
    }

    catalogue = frozenset(par_slug)
    validation = valider(programme, contraintes, regles, catalogue_autorise=catalogue)
    resultat["validation"] = vue_validation(validation)

    payload = construire_contexte(programme, contraintes)
    violations = verifier_contexte(payload, profil.valeurs_sensibles)
    resultat["contexte"] = {
        "payload": payload,
        "octets": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        "violations": violations,
        "donnees_interdites_absentes": not violations,
    }

    repli = justification_de_repli(programme, contraintes)
    redaction: dict[str, Any] = {
        "mode": "repli",
        "texte": repli,
        "llm": {"statut": "desactive", "motif": MOTIF_LLM_DESACTIVE},
        "verification": None,
        "repli_declenche": False,
    }
    selection_avant = [(r["meal_id"], r["slug"]) for j in resultat["composition"]["jours"] for r in j["repas"]]

    if reponse_llm is not None:
        verification = verifier_reponse_llm(reponse_llm, payload)
        redaction.update(mode="reponse_simulee", verification=verification)
        if verification["valide"]:
            redaction["texte"] = reponse_llm["plan_summary"]
        else:
            redaction["repli_declenche"] = True

    # FN-022 — rejouée après la rédaction : le texte arrive après la sélection.
    validation_apres = valider(programme, contraintes, regles, catalogue_autorise=catalogue)
    selection_apres = [
        (meal_id(j.index, r.slot), r.plat.slug)
        for j in programme.journees
        for r in sorted(j.repas, key=lambda r: ORDRE_CRENEAUX.index(r.slot.value))
    ]
    resultat["redaction"] = redaction
    resultat["apres_llm"] = {
        "selection_inchangee": selection_avant == selection_apres,
        "validation": vue_validation(validation_apres),
        "explication": (
            "Le texte ne peut pas modifier la sélection : seuls les meal_id fournis sont acceptés, "
            "et le programme est revalidé après la rédaction."
        ),
    }
    resultat["duree_ms"] = round((time.perf_counter() - debut) * 1000, 2)
    return resultat


def comparer(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Créneau par créneau : quel plat change, et de combien le score bouge."""

    def index(simulation: dict[str, Any]) -> dict[tuple[int, str], dict[str, Any]]:
        composition = simulation.get("composition") or {}
        return {
            (jour["index"], repas["slot"]): repas
            for jour in composition.get("jours", [])
            for repas in jour["repas"]
        }

    ia, ib = index(a), index(b)
    lignes = []
    for cle in sorted(set(ia) | set(ib), key=lambda c: (c[0], ORDRE_CRENEAUX.index(c[1]))):
        ra, rb = ia.get(cle), ib.get(cle)
        lignes.append(
            {
                "jour": cle[0],
                "slot": cle[1],
                "creneau": LIBELLES_CRENEAUX[cle[1]],
                "a": ra and {"slug": ra["slug"], "nom": ra["nom"], "score": ra["score"]},
                "b": rb and {"slug": rb["slug"], "nom": rb["nom"], "score": rb["score"]},
                "change": (ra or {}).get("slug") != (rb or {}).get("slug"),
            }
        )
    return {
        "statut_a": (a.get("composition") or {}).get("statut"),
        "statut_b": (b.get("composition") or {}).get("statut"),
        "creneaux": len(lignes),
        "changements": sum(1 for l in lignes if l["change"]),
        "lignes": lignes,
    }


__all__ = [
    "CATALOGUE_FICTIF",
    "CLES_INTERDITES_LLM",
    "CatalogueInvalide",
    "LIBELLES_CONTROLES",
    "LIBELLES_CRENEAUX",
    "MOTIF_LLM_DESACTIVE",
    "ProfilEssai",
    "ProfilInvalide",
    "charger_catalogue_fictif",
    "comparer",
    "construire_contexte",
    "ingredients_du_catalogue",
    "meal_id",
    "profil_depuis",
    "simuler",
    "verifier_contexte",
    "verifier_reponse_llm",
    "vue_regles",
    "vue_validation",
]
