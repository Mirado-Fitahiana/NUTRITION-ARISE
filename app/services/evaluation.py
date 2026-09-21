"""Cas d'évaluation du laboratoire — des résultats testables (plan §8.4).

**Un seul exécutant, deux usages** : `tests/test_evaluation.py` rejoue tous les
cas à chaque commit, et l'écran « Lancer la campagne » les rejoue à la demande.
Les deux utilisent le catalogue fictif et les valeurs par défaut du registre :
ils rendent donc exactement les mêmes verdicts.

Une attente inconnue (faute de frappe dans le YAML) fait **échouer** le cas :
une assertion qui ne vérifie rien ne doit pas passer au vert.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.services import reglages
from app.services.laboratoire import ProfilInvalide, profil_depuis, simuler
from app.services.recommandation import POIDS_PAR_DEFAUT, PlatCandidat

ROOT = Path(__file__).resolve().parents[2]
DOSSIER_CAS = ROOT / "evaluation" / "cas"


class CasInvalide(ValueError):
    pass


@dataclass(frozen=True)
class Cas:
    id: str
    titre: str
    description: str
    fichier: str
    profil: dict[str, Any]
    jours: int
    seed: int
    poids: dict[str, Any] | None
    regles: dict[str, Any] | None
    attendu: dict[str, Any]
    reponse_llm: Any = None


def charger_cas(dossier: Path = DOSSIER_CAS) -> list[Cas]:
    tous: list[Cas] = []
    ids: set[str] = set()
    for fichier in sorted(dossier.glob("*.yaml")):
        contenu = yaml.safe_load(fichier.read_text(encoding="utf-8")) or []
        for entree in contenu if isinstance(contenu, list) else [contenu]:
            if not isinstance(entree, dict) or "id" not in entree or not entree.get("attendu"):
                raise CasInvalide(f"{fichier.name} : cas sans id ou sans attendu")
            identifiant = str(entree["id"])
            if identifiant in ids:
                raise CasInvalide(f"id de cas en double : {identifiant}")
            ids.add(identifiant)
            tous.append(
                Cas(
                    id=identifiant,
                    titre=str(entree.get("titre") or identifiant),
                    description=str(entree.get("description") or "").strip(),
                    fichier=fichier.name,
                    profil=dict(entree.get("profil") or {}),
                    jours=int(entree.get("jours", 1)),
                    seed=int(entree.get("seed", 42)),
                    poids=entree.get("poids"),
                    regles=entree.get("regles"),
                    attendu=dict(entree["attendu"]),
                    reponse_llm=entree.get("reponse_llm"),
                )
            )
    return tous


def _assertion(nom: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"nom": nom, "ok": bool(ok), "detail": detail}


def _signature(simulation: dict[str, Any]) -> list[Any]:
    composition = simulation["composition"]
    if composition["statut"] != "ok":
        return ["echec", composition["echec"]["code"]]
    return [
        (jour["index"], repas["slot"], repas["slug"])
        for jour in composition["jours"]
        for repas in jour["repas"]
    ]


def executer_cas(cas: Cas, plats: list[PlatCandidat]) -> dict[str, Any]:
    debut = time.perf_counter()
    entete = {
        "id": cas.id,
        "titre": cas.titre,
        "description": cas.description,
        "fichier": cas.fichier,
    }
    try:
        profil = profil_depuis(cas.profil)
        regles = reglages.regles_depuis(reglages.resoudre({}), cas.regles)
        poids = (
            reglages.valider_poids(cas.poids) if cas.poids is not None else dict(POIDS_PAR_DEFAUT)
        )
    except (ProfilInvalide, reglages.ReglageInvalide, ValueError) as exc:
        return {
            **entete,
            "ok": False,
            "composition": None,
            "assertions": [_assertion("preparation", False, str(exc))],
            "duree_ms": round((time.perf_counter() - debut) * 1000, 2),
        }

    def lancer() -> dict[str, Any]:
        return simuler(
            plats,
            profil,
            jours=cas.jours,
            regles=regles,
            poids=poids,
            seed=cas.seed,
            reponse_llm=cas.reponse_llm,
        )

    simulation = lancer()
    composition = simulation["composition"]
    succes = composition["statut"] == "ok"
    code_echec = None if succes else composition["echec"]["code"]
    par_slug = {p.slug: p for p in plats}
    sans_programme = "aucun programme à vérifier : " + (code_echec or "")

    assertions: list[dict[str, Any]] = []
    for nom, attendu in cas.attendu.items():
        if nom == "succes":
            assertions.append(
                _assertion(nom, succes == bool(attendu), f"composition : {code_echec or 'réussie'}")
            )
        elif nom == "echec":
            assertions.append(
                _assertion(nom, code_echec == attendu, f"attendu {attendu}, obtenu {code_echec or 'un programme'}")
            )
        elif nom == "aucun_allergene":
            interdits = {str(a) for a in attendu}
            exclus = {e["slug"] for e in simulation["vivier"]["exclus"]}
            fautifs: set[str] = set()
            for plat in plats:
                if plat.slug not in exclus and {a.value for a in plat.allergens} & interdits:
                    fautifs.add(f"vivier:{plat.slug}")
            for creneau in simulation["score"]["creneaux"]:
                for candidat in creneau["candidats"]:
                    allergenes = {a.value for a in par_slug[candidat["slug"]].allergens}
                    if allergenes & interdits:
                        fautifs.add(f"candidat:{candidat['slug']}")
            if succes:
                for jour in composition["jours"]:
                    for repas in jour["repas"]:
                        if set(repas["allergenes"]) & interdits:
                            fautifs.add(f"programme:{repas['slug']}")
            assertions.append(
                _assertion(
                    nom,
                    not fautifs,
                    "aucun plat concerné, à aucune étape" if not fautifs else ", ".join(sorted(fautifs)),
                )
            )
        elif nom == "restrictions_respectees":
            if not succes:
                assertions.append(_assertion(nom, False, sans_programme))
                continue
            controle = next(c for c in simulation["validation"]["controles"] if c["numero"] == 2)
            assertions.append(
                _assertion(nom, (controle["statut"] == "ok") == bool(attendu), f"contrôle 2 : {controle['statut']}")
            )
        elif nom == "validation_conforme":
            if not succes:
                assertions.append(_assertion(nom, False, sans_programme))
                continue
            conforme = simulation["validation"]["conforme"]
            assertions.append(_assertion(nom, conforme == bool(attendu), f"conforme : {conforme}"))
        elif nom == "incident_securite":
            incident = bool(simulation["validation"]["incident_securite"]) if succes else False
            assertions.append(_assertion(nom, incident == bool(attendu), f"incident : {incident}"))
        elif nom == "reproductible":
            identique = _signature(simulation) == _signature(lancer())
            assertions.append(
                _assertion(nom, identique == bool(attendu), "même graine, même programme" if identique else "résultats différents")
            )
        elif nom == "creneaux_non_couverts_max":
            if not succes:
                assertions.append(_assertion(nom, False, sans_programme))
                continue
            n = len(composition["non_couverts"])
            assertions.append(_assertion(nom, n <= int(attendu), f"{n} créneau(x) non couvert(s)"))
        elif nom == "contexte_sans_donnees_interdites":
            if not succes:
                assertions.append(_assertion(nom, False, sans_programme))
                continue
            violations = simulation["contexte"]["violations"]
            assertions.append(
                _assertion(nom, (not violations) == bool(attendu), "; ".join(violations) or "aucune donnée interdite")
            )
        elif nom == "reponse_llm_valide":
            verification = (simulation.get("redaction") or {}).get("verification")
            if verification is None:
                assertions.append(_assertion(nom, False, "aucune réponse simulée à vérifier"))
                continue
            assertions.append(
                _assertion(
                    nom,
                    verification["valide"] == bool(attendu),
                    "réponse acceptée" if verification["valide"] else "; ".join(verification["erreurs"]),
                )
            )
        elif nom == "reponse_llm_erreurs_contiennent":
            verification = (simulation.get("redaction") or {}).get("verification") or {"erreurs": []}
            texte = " | ".join(verification["erreurs"])
            manquants = [m for m in attendu if m not in texte]
            assertions.append(
                _assertion(nom, not manquants, texte or "aucune erreur" if not manquants else f"absent : {manquants}")
            )
        else:
            assertions.append(_assertion(nom, False, "attente inconnue — vérifier l'orthographe du cas"))

    return {
        **entete,
        "ok": bool(assertions) and all(a["ok"] for a in assertions),
        "composition": code_echec or "ok",
        "assertions": assertions,
        "duree_ms": round((time.perf_counter() - debut) * 1000, 2),
    }


def executer_campagne(cas: list[Cas], plats: list[PlatCandidat]) -> dict[str, Any]:
    debut = time.perf_counter()
    resultats = [executer_cas(c, plats) for c in cas]
    reussis = sum(1 for r in resultats if r["ok"])
    return {
        "total": len(resultats),
        "reussis": reussis,
        "echoues": len(resultats) - reussis,
        "cas": resultats,
        "conditions": "catalogue fictif · valeurs par défaut du registre · poids par défaut sauf mention du cas",
        "duree_ms": round((time.perf_counter() - debut) * 1000, 2),
    }


__all__ = ["Cas", "CasInvalide", "DOSSIER_CAS", "charger_cas", "executer_campagne", "executer_cas"]
