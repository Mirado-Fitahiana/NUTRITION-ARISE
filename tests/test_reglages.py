"""Registre des réglages (plan §7, FN-020, FN-024).

Deux régressions sont visées :

* **le repli silencieux** — une valeur invalide en base doit retomber sur le
  défaut *et* être signalée, jamais ignorée sans trace ;
* **le réglage fantôme** — un poids ou un paramètre que rien ne lit ne doit pas
  pouvoir être enregistré comme s'il agissait.
"""

import inspect
from decimal import Decimal

import pytest

from app.routers import planning
from app.services import reglages
from app.services.recommandation import POIDS_PAR_DEFAUT, ReglesComposition

PAR_CLE = reglages.PAR_CLE


class TestResolution:
    def test_les_defauts_sont_ceux_de_l_ancien_repli(self):
        """Brancher le registre ne change aucune génération tant que la base est vide."""
        assert reglages.regles_par_defaut() == ReglesComposition()

    def test_valeur_hors_bornes_rejetee_et_signalee(self):
        etat = reglages.resoudre({"min_pool_size": {"value": 0}})["min_pool_size"]
        assert etat.source == "rejete"
        assert etat.effectif == 25
        assert "minimum" in etat.motif

    def test_valeur_valide_appliquee(self):
        etat = reglages.resoudre({"min_pool_size": {"value": 12}})["min_pool_size"]
        assert (etat.source, etat.effectif) == ("base", 12)

    def test_formats_avec_et_sans_enveloppe(self):
        repartition = {"breakfast": "0.3", "lunch": "0.4", "dinner": "0.3"}
        nu = reglages.resoudre({"meal_distribution": {"value": repartition}})["meal_distribution"]
        enveloppe = reglages.resoudre({"meal_distribution": {"value": {"value": repartition}}})["meal_distribution"]
        assert nu.source == enveloppe.source == "base"
        assert nu.effectif == enveloppe.effectif

    def test_seuils_de_fraicheur_ordonnes(self):
        etats = reglages.resoudre({"price_recent_days": {"value": 50}})
        assert etats["price_recent_days"].source == "rejete"
        assert etats["price_recent_days"].effectif == 14

    def test_cles_inconnues_signalees(self):
        assert reglages.cles_inconnues({"w_objectif": {}, "min_pool_size": {}}) == ["w_objectif"]


class TestValidation:
    def test_repartition_doit_sommer_a_un(self):
        with pytest.raises(reglages.ReglageInvalide, match="somme"):
            reglages.valider(PAR_CLE["meal_distribution"], {"breakfast": "0.5", "lunch": "0.6"})

    def test_creneau_inconnu_refuse(self):
        with pytest.raises(reglages.ReglageInvalide, match="créneau inconnu"):
            reglages.valider(PAR_CLE["meal_distribution"], {"brunch": "1"})

    def test_entier_attendu(self):
        with pytest.raises(reglages.ReglageInvalide, match="entier"):
            reglages.valider(PAR_CLE["max_repeats"], "2.5")

    def test_booleen_strict(self):
        with pytest.raises(reglages.ReglageInvalide):
            reglages.valider(PAR_CLE["scraping_write_enabled"], "true")

    def test_le_delai_de_collecte_ne_descend_pas_sous_le_plancher(self):
        with pytest.raises(reglages.ReglageInvalide, match="minimum"):
            reglages.valider(PAR_CLE["scraping_min_delay_s"], "1")

    @pytest.mark.parametrize(
        "cle", ["meal_kcal_tolerance", "price_recent_days", "price_stale_days", "scraping_write_enabled", "llm_monthly_budget"]
    )
    def test_reglages_non_branches_marques(self, cle):
        assert PAR_CLE[cle].branche is False

    def test_surcharges_du_laboratoire_passent_par_la_validation(self):
        with pytest.raises(reglages.ReglageInvalide):
            reglages.regles_depuis(reglages.resoudre({}), {"min_pool_size": 0})
        assert reglages.regles_depuis(reglages.resoudre({}), {"min_pool_size": 10}).min_pool_size == 10


class TestPoids:
    def test_cles_autorisees_sont_celles_du_moteur(self):
        assert reglages.POIDS_AUTORISES == tuple(POIDS_PAR_DEFAUT)

    def test_terme_prevu_mais_non_lu_refuse(self):
        poids = {**{c: "0.2" for c in reglages.POIDS_AUTORISES}, "w_local": "1"}
        with pytest.raises(reglages.ReglageInvalide, match="scorer"):
            reglages.valider_poids(poids)

    def test_jeu_incomplet_refuse(self):
        with pytest.raises(reglages.ReglageInvalide, match="manquants"):
            reglages.valider_poids({"nutrition": "1"})

    @pytest.mark.parametrize("valeur", ["-0.1", "5.5"])
    def test_poids_hors_bornes_refuse(self, valeur):
        with pytest.raises(reglages.ReglageInvalide):
            reglages.valider_poids({**{c: "0.2" for c in reglages.POIDS_AUTORISES}, "cost": valeur})

    def test_jeu_entierement_nul_refuse(self):
        with pytest.raises(reglages.ReglageInvalide, match="non nul"):
            reglages.valider_poids({c: "0" for c in reglages.POIDS_AUTORISES})

    def test_lecture_ignore_et_signale(self):
        poids, ignores = reglages.poids_depuis({"nutrition": "0.5", "w_objectif": 1, "cost": "-1"})
        assert poids["nutrition"] == Decimal("0.5")
        assert poids["cost"] == POIDS_PAR_DEFAUT["cost"]
        assert ignores == ["cost", "w_objectif"]

    @pytest.mark.parametrize("version", ["fallback-1", "V2", "", "a" * 33])
    def test_versions_refusees(self, version):
        with pytest.raises(reglages.ReglageInvalide):
            reglages.valider_version(version)

    def test_version_acceptee(self):
        assert reglages.valider_version(" v2.1 ") == "v2.1"


def test_la_generation_lit_le_registre():
    """Structurel : `planning` ne doit pas revenir à une lecture de
    `app_settings` qui contournerait la validation."""
    assert "reglages.regles_depuis" in inspect.getsource(planning._regles)
    assert "reglages.poids_depuis" in inspect.getsource(planning._poids)
