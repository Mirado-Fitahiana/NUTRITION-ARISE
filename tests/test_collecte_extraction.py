"""Lecture des prix, des conditionnements, et connecteurs (FN-013).

Ce que ces tests protègent : **rien n'est deviné.** Un prix sans devise, deux
montants dans la même case, un séparateur ambigu restent vides, avec leur motif.
Un prix faux inséré en silence ne produit aucune erreur visible — il produit une
liste de courses fausse.

Les pages de `tests/fixtures/html` sont des reconstitutions du balisage des deux
plateformes, pas des captures des sites : aucun test ne sollicite le réseau.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from app.services.collecte.connecteurs import connecteur_pour, plateformes
from app.services.collecte.extraction import (
    PRIX_ABSENT,
    PRIX_ILLISIBLE,
    PRIX_LU,
    lire_conditionnement,
    lire_prix,
    taux_echec,
)

FIXTURES = Path(__file__).parent / "fixtures" / "html"


class TestPrix:
    @pytest.mark.parametrize(
        ("texte", "attendu"),
        [
            ("23 900 Ar", Decimal("23900.00")),
            ("23 900 Ar", Decimal("23900.00")),
            ("Ar 4 900", Decimal("4900.00")),
            ("1 250,50 Ar", Decimal("1250.50")),
            ("12.500 Ar", Decimal("12500.00")),
            ("15000 MGA", Decimal("15000.00")),
            ("2 000 Ariary", Decimal("2000.00")),
        ],
    )
    def test_montants_lisibles(self, texte, attendu):
        prix = lire_prix(texte)
        assert prix.statut == PRIX_LU
        assert prix.montant == attendu
        assert prix.devise == "MGA"

    @pytest.mark.parametrize(
        ("texte", "motif"),
        [
            ("Prix sur demande", "aucun montant"),
            ("8 400 Ar – 23 400 Ar", "plusieurs montants"),
            ("23 900", "devise absente"),
            ("12,99 €", "devise"),
            ("0 Ar", "nul"),
            ("1.2345 Ar", "ambigu"),
        ],
    )
    def test_prix_illisible_jamais_devine(self, texte, motif):
        prix = lire_prix(texte)
        assert prix.statut == PRIX_ILLISIBLE
        assert prix.montant is None
        assert motif in prix.motif

    @pytest.mark.parametrize("texte", [None, "", "   "])
    def test_aucun_prix_affiche_n_est_pas_un_echec(self, texte):
        assert lire_prix(texte).statut == PRIX_ABSENT

    def test_taux_d_echec_ignore_les_prix_absents(self):
        assert taux_echec(lus=8, illisibles=2) == Decimal("0.200")
        # Zéro sur zéro n'est pas un taux.
        assert taux_echec(lus=0, illisibles=0) is None


class TestConditionnement:
    @pytest.mark.parametrize(
        ("libelle", "quantite", "unite"),
        [
            ("Sucre blanc cristallisé 1kg", Decimal("1"), "kg"),
            ("Sucre roux 500 g", Decimal("500"), "g"),
            ("Huile végétale 1 L", Decimal("1"), "l"),
            ("Lait 6 x 1 l", Decimal("6"), "l"),
            ("Lentilles corail 500g", Decimal("500"), "g"),
            ("Riz local 2,5 kg", Decimal("2.5"), "kg"),
        ],
    )
    def test_conditionnements_lisibles(self, libelle, quantite, unite):
        lu = lire_conditionnement(libelle)
        assert lu.quantite == quantite
        assert lu.unite == unite

    @pytest.mark.parametrize(
        "libelle", ["Sucre vanillé x 10", "Pack 1 kg + 500 g offerts", "Panier solidaire", "Mangue 2 gros fruits"]
    )
    def test_conditionnement_ambigu_ou_absent_reste_vide(self, libelle):
        assert lire_conditionnement(libelle).quantite is None


class TestConnecteurs:
    def test_plateformes_connues(self):
        assert plateformes() == ["prestashop", "woocommerce"]

    def test_plateforme_inconnue_refusee(self):
        with pytest.raises(ValueError):
            connecteur_pour("shopify")


class TestPrestaShop:
    @pytest.fixture(scope="class")
    def resultat(self):
        html = (FIXTURES / "prestashop_categorie.html").read_text(encoding="utf-8")
        return connecteur_pour("prestashop").extraire(html, "https://www.kibo.mg/tananarive/182-sucres")

    def test_blocs_et_libelles(self, resultat):
        assert resultat.blocs_produit == 5
        assert [o.libelle for o in resultat.offres][:2] == ["Sucre blanc cristallisé 1kg", "Sucre roux 500 g"]

    def test_prix_courant_et_non_le_prix_barre(self, resultat):
        promotion = next(o for o in resultat.offres if o.libelle == "Sucre roux 500 g")
        assert promotion.prix_texte == "5 900 Ar"

    def test_produit_sans_prix_affiche(self, resultat):
        sans_prix = next(o for o in resultat.offres if o.libelle.startswith("Sucre de canne"))
        assert sans_prix.prix_texte is None

    def test_liens_relatifs_resolus(self, resultat):
        assert resultat.offres[0].url == (
            "https://www.kibo.mg/tananarive/sucres/101-sucre-blanc-cristallise-1kg.html"
        )

    def test_lien_javascript_ecarte_et_libelle_garde_comme_texte(self, resultat):
        piege = next(o for o in resultat.offres if o.libelle.startswith("Sucre vanillé"))
        assert piege.url is None
        assert "<img" in piege.libelle  # du texte, que la plateforme affiche sans l'interpréter


class TestWooCommerce:
    @pytest.fixture(scope="class")
    def offres(self):
        html = (FIXTURES / "woocommerce_boutique.html").read_text(encoding="utf-8")
        resultat = connecteur_pour("woocommerce").extraire(html, "https://www.abcie.org/boutique/")
        assert resultat.blocs_produit == 6
        return {o.libelle: o for o in resultat.offres}

    def test_prix_simple(self, offres):
        assert lire_prix(offres["Riz local 5 kg"].prix_texte).montant == Decimal("23400.00")

    def test_promotion_seul_le_prix_courant(self, offres):
        assert offres["Huile végétale 1 L"].prix_texte == "8 400 Ar"

    def test_fourchette_rendue_ambigue(self, offres):
        assert lire_prix(offres["Haricots rouges (vrac)"].prix_texte).statut == PRIX_ILLISIBLE

    def test_prix_sur_demande_illisible_et_absence_distinguees(self, offres):
        assert lire_prix(offres["Épices du marché"].prix_texte).statut == PRIX_ILLISIBLE
        assert lire_prix(offres["Panier solidaire"].prix_texte).statut == PRIX_ABSENT

    def test_rupture_de_stock(self, offres):
        assert offres["Lentilles corail 500g"].disponibilite == "rupture"
        assert offres["Lentilles corail 500g"].url == "https://www.abcie.org/produit/lentilles-corail/"
