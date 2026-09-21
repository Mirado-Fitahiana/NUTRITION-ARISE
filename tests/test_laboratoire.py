"""Laboratoire de recommandation — le « RAG » d'ARISE (plan §8, D-06, FN-021).

Trois garanties :

1. **un allergène n'atteint aucune étape** — ni le vivier, ni les candidats
   notés, ni le programme ;
2. **le contexte transmis au modèle ne contient aucune donnée interdite**, même
   quand le profil d'essai en porte ;
3. **une réponse de modèle fautive est rejetée** et ne peut pas modifier la
   sélection.

Et une garantie sur le moteur lui-même : la trace ajoutée à `composer` pour le
laboratoire n'influe sur aucune décision.
"""

import ast
import json
from pathlib import Path

import pytest

from app.services import laboratoire, reglages
from app.services.laboratoire import (
    CatalogueInvalide,
    ProfilInvalide,
    charger_catalogue_fictif,
    comparer,
    profil_depuis,
    simuler,
    verifier_contexte,
    verifier_reponse_llm,
)
from app.services.recommandation import POIDS_PAR_DEFAUT, composer, filtrer

PLATS, INFO = charger_catalogue_fictif()
REGLES = reglages.regles_par_defaut()
POIDS = dict(POIDS_PAR_DEFAUT)
EQUILIBRE = {"goal": "balanced_diet", "kcal_target": 2000}


def simulation(profil, jours=3, seed=42, reponse=None):
    return simuler(PLATS, profil_depuis(profil), jours=jours, regles=REGLES, poids=POIDS, seed=seed, reponse_llm=reponse)


class TestCatalogueFictif:
    def test_charge_et_se_declare_fictif(self):
        assert INFO["fictif"] is True
        assert INFO["total"] == 42

    def test_refuse_un_fichier_qui_ne_se_declare_pas_fictif(self, tmp_path):
        fichier = tmp_path / "catalogue.yaml"
        fichier.write_text("plats: []\n", encoding="utf-8")
        with pytest.raises(CatalogueInvalide):
            charger_catalogue_fictif(fichier)


class TestSecurite:
    def test_allergene_absent_de_toutes_les_etapes(self):
        s = simulation({**EQUILIBRE, "allergens": ["peanut"]}, jours=7)
        arachide = {p.slug for p in PLATS if any(a.value == "peanut" for a in p.allergens)}
        assert arachide, "garde-fou : le catalogue fictif doit contenir de l'arachide"

        assert arachide <= {e["slug"] for e in s["vivier"]["exclus"]}
        assert not {c["slug"] for cr in s["score"]["creneaux"] for c in cr["candidats"]} & arachide
        assert not {r["slug"] for j in s["composition"]["jours"] for r in j["repas"]} & arachide
        assert s["validation"]["incident_securite"] is False

    def test_le_contexte_ne_transmet_aucune_donnee_de_sante(self):
        s = simulation(
            {
                "goal": "weight_loss",
                "physio": {
                    "weight_kg": "68",
                    "height_cm": "165",
                    "birth_date": "1994-03-12",
                    "sex": "female",
                    "activity_level": "lightly_active",
                },
            },
            jours=2,
        )
        assert s["composition"]["statut"] == "ok"
        assert s["contexte"]["violations"] == []
        assert "1994-03-12" not in json.dumps(s["contexte"]["payload"])

    def test_la_verification_detecte_une_fuite(self):
        violations = verifier_contexte(
            {"objectif": "weight_loss", "profil": {"birth_date": "1994-03-12"}, "note": "née le 1994-03-12"},
            ("1994-03-12",),
        )
        assert any("birth_date" in v for v in violations)
        assert any("valeur de profil" in v for v in violations)


class TestReponseDuModele:
    @pytest.fixture(scope="class")
    def payload(self):
        return simulation(EQUILIBRE, jours=1)["contexte"]["payload"]

    @staticmethod
    def conforme(payload):
        return {
            "plan_summary": "Une journée équilibrée.",
            "days": [
                {"day": j["jour"], "meals": [{"meal_id": r["meal_id"], "justification": "Un choix du moteur."} for r in j["repas"]]}
                for j in payload["jours"]
            ],
        }

    def test_reponse_conforme_acceptee(self, payload):
        verification = verifier_reponse_llm(self.conforme(payload), payload)
        assert verification["valide"], verification["erreurs"]
        assert verification["meal_ids_couverts"] == verification["meal_ids_attendus"] == 5

    def test_identifiant_inconnu_rejete(self, payload):
        reponse = self.conforme(payload)
        reponse["days"][0]["meals"].append({"meal_id": "J9-dinner", "justification": "Inventé."})
        verification = verifier_reponse_llm(reponse, payload)
        assert not verification["valide"]
        assert verification["meal_ids_inconnus"] == ["J9-dinner"]

    def test_chiffre_rejete(self, payload):
        reponse = self.conforme(payload)
        reponse["days"][0]["meals"][0]["justification"] = "Environ 650 kcal."
        assert any("chiffre" in e for e in verifier_reponse_llm(reponse, payload)["erreurs"])

    def test_champ_inattendu_rejete(self, payload):
        reponse = {**self.conforme(payload), "kcal_total": "beaucoup"}
        assert any("champ inattendu" in e for e in verifier_reponse_llm(reponse, payload)["erreurs"])

    def test_reponse_non_objet_rejetee(self, payload):
        assert not verifier_reponse_llm(["pas", "un", "objet"], payload)["valide"]

    def test_une_reponse_rejetee_ne_modifie_pas_la_selection(self):
        sans = simulation(EQUILIBRE, jours=1)
        fautive = {"plan_summary": "x", "days": [{"day": 1, "meals": [{"meal_id": "J9-dinner", "justification": "Inventé."}]}]}
        avec = simulation(EQUILIBRE, jours=1, reponse=fautive)
        assert avec["redaction"]["repli_declenche"] is True
        assert avec["apres_llm"]["selection_inchangee"] is True
        assert avec["composition"] == sans["composition"]


class TestMoteur:
    def test_la_trace_n_influe_pas_sur_la_composition(self):
        profil = profil_depuis(EQUILIBRE)
        vivier = filtrer(PLATS, profil.contraintes)
        sans = composer(vivier, profil.contraintes, 7, REGLES, poids=POIDS, seed=5)
        trace = []
        avec = composer(vivier, profil.contraintes, 7, REGLES, poids=POIDS, seed=5, trace=trace)
        assert sans == avec
        assert len(trace) == 7 * 5

    def test_echec_explique_sans_etapes_suivantes(self):
        s = simulation({**EQUILIBRE, "restrictions": ["vegan"]}, jours=7)
        assert s["composition"]["statut"] == "echec"
        assert s["composition"]["echec"]["code"] == "CATALOG_TOO_SMALL"
        assert s["validation"] is s["contexte"] is s["redaction"] is None

    def test_meme_graine_meme_programme(self):
        assert simulation(EQUILIBRE, seed=9)["composition"] == simulation(EQUILIBRE, seed=9)["composition"]

    def test_comparaison_par_creneau(self):
        resultat = comparer(simulation(EQUILIBRE, seed=1), simulation(EQUILIBRE, seed=2))
        assert resultat["creneaux"] == 3 * 5
        assert 0 <= resultat["changements"] <= resultat["creneaux"]

    def test_profil_sans_cible_refuse(self):
        with pytest.raises(ProfilInvalide):
            profil_depuis({"goal": "balanced_diet"})


def test_le_laboratoire_n_accede_pas_a_la_base():
    """Structurel : le service n'importe ni SQLAlchemy ni la session. Il ne
    peut rien écrire, pas seulement il n'écrit rien."""
    arbre = ast.parse(Path(laboratoire.__file__).read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(arbre) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(arbre) if isinstance(n, ast.Import) for a in n.names}
    interdits = {m for m in modules if m and (m.startswith("sqlalchemy") or m in {"app.core.database", "app.models.planning"})}
    assert not interdits
