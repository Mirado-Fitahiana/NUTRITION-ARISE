"""Règles de prix (FN-015, FN-016, D-09).

Ce que ces tests tiennent, c'est une classe de bugs silencieux : un prix périmé
qui se glisse dans un budget, une médiane calculée à l'envers, une fourchette à
zéro qui se lit comme « gratuit ». Rien de tout cela ne lève d'exception — cela
produit une liste de courses fausse que personne ne remarque.

Aucune base n'est nécessaire.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.enums import PriceFreshness, PriceSource, ReferenceUnit
from app.services.pricing import (
    SEUIL_ANCIEN_JOURS,
    SEUIL_RECENT_JOURS,
    Observation,
    agreger,
    apparier_ingredient,
    confiance,
    est_aberrant,
    fraicheur,
    mediane,
    prix_par_unite_de_reference,
)
from app.services.units import IngredientConversion, UnitConversionError

MAINTENANT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def obs(prix: str, jours: int = 0, source: PriceSource = PriceSource.MANUAL) -> Observation:
    return Observation(
        price=Decimal(prix),
        collected_at=MAINTENANT - timedelta(days=jours),
        source=source,
    )


class TestFraicheur:
    @pytest.mark.parametrize(
        "jours,attendu",
        [
            (0, PriceFreshness.RECENT),
            (SEUIL_RECENT_JOURS, PriceFreshness.RECENT),
            (SEUIL_RECENT_JOURS + 1, PriceFreshness.STALE),
            (SEUIL_ANCIEN_JOURS, PriceFreshness.STALE),
            (SEUIL_ANCIEN_JOURS + 1, PriceFreshness.OBSOLETE),
            (365, PriceFreshness.OBSOLETE),
        ],
    )
    def test_seuils(self, jours, attendu):
        date = MAINTENANT - timedelta(days=jours)
        assert fraicheur(date, maintenant=MAINTENANT) == attendu

    def test_date_future_traitee_comme_recente(self):
        """Une date dans le futur est une faute de saisie, pas une donnée
        périmée : l'écarter silencieusement la rendrait invisible."""
        futur = MAINTENANT + timedelta(days=3)
        assert fraicheur(futur, maintenant=MAINTENANT) == PriceFreshness.RECENT

    def test_seuils_configurables(self):
        date = MAINTENANT - timedelta(days=20)
        assert fraicheur(date, maintenant=MAINTENANT) == PriceFreshness.STALE
        assert (
            fraicheur(date, maintenant=MAINTENANT, seuil_recent=30)
            == PriceFreshness.RECENT
        )


class TestMediane:
    def test_nombre_impair(self):
        assert mediane([Decimal("1"), Decimal("3"), Decimal("2")]) == Decimal("2")

    def test_nombre_pair_interpole(self):
        assert mediane([Decimal("10"), Decimal("20")]) == Decimal("15")

    def test_liste_vide_refusee(self):
        with pytest.raises(ValueError):
            mediane([])


class TestAgregation:
    def test_sans_observation_renvoie_none(self):
        """Pas une fourchette à zéro, qui se lirait comme un prix gratuit."""
        assert agreger([]) is None

    def test_observation_unique(self):
        f = agreger([obs("5000")], maintenant=MAINTENANT)

        assert f is not None
        assert f.minimum == f.mediane == f.maximum == Decimal("5000")
        assert f.observations == 1
        assert f.freshness == PriceFreshness.RECENT

    def test_fourchette_ordonnee(self):
        prix = ["1000", "2000", "3000", "4000", "5000", "6000", "7000"]
        f = agreger([obs(p) for p in prix], maintenant=MAINTENANT)

        assert f is not None
        assert f.minimum <= f.mediane <= f.maximum
        assert f.mediane == Decimal("4000")

    def test_les_obsoletes_sont_ecartees(self):
        """FN-016 — un prix de plus de 45 jours ne chiffre plus rien."""
        f = agreger(
            [obs("1000", jours=100), obs("5000", jours=1)], maintenant=MAINTENANT
        )

        assert f is not None
        assert f.observations == 1
        assert f.mediane == Decimal("5000")

    def test_toutes_obsoletes_renvoie_none(self):
        assert agreger([obs("1000", jours=100)], maintenant=MAINTENANT) is None

    def test_obsoletes_incluses_sur_demande(self):
        """L'écran d'administration doit pouvoir montrer l'historique."""
        f = agreger(
            [obs("1000", jours=100)], maintenant=MAINTENANT, inclure_obsoletes=True
        )

        assert f is not None
        assert f.observations == 1
        assert f.freshness == PriceFreshness.OBSOLETE
        assert f.utilisable_pour_budget is False

    def test_date_retenue_est_la_plus_recente(self):
        f = agreger([obs("1000", jours=30), obs("2000", jours=2)], maintenant=MAINTENANT)

        assert f is not None
        assert f.observed_at == MAINTENANT - timedelta(days=2)
        assert f.freshness == PriceFreshness.RECENT

    def test_stale_reste_utilisable_pour_le_budget(self):
        f = agreger([obs("1000", jours=20)], maintenant=MAINTENANT)

        assert f is not None
        assert f.freshness == PriceFreshness.STALE
        assert f.utilisable_pour_budget is True


class TestConfiance:
    def test_sans_observation_est_nulle(self):
        assert confiance([]) == Decimal("0")

    def test_croit_avec_le_nombre_d_observations(self):
        une = confiance([obs("1000")], maintenant=MAINTENANT)
        trois = confiance([obs("1000")] * 3, maintenant=MAINTENANT)

        assert trois > une

    def test_decroit_avec_l_age(self):
        recente = confiance([obs("1000", jours=1)], maintenant=MAINTENANT)
        ancienne = confiance([obs("1000", jours=30)], maintenant=MAINTENANT)

        assert ancienne < recente

    def test_le_maillon_faible_fixe_la_methode(self):
        """Mélanger un relevé manuel et une donnée communautaire ne vaut pas
        mieux que la source la moins fiable."""
        manuel = confiance([obs("1000"), obs("1000")], maintenant=MAINTENANT)
        mixte = confiance(
            [obs("1000"), obs("1000", source=PriceSource.COMMUNITY)],
            maintenant=MAINTENANT,
        )

        assert mixte < manuel

    def test_bornee_entre_zero_et_un(self):
        beaucoup = confiance([obs("1000")] * 50, maintenant=MAINTENANT)

        assert Decimal("0") <= beaucoup <= Decimal("1")


class TestConversionVersUniteDeReference:
    def test_conversion_directe(self):
        """1 kg à 4000 Ar → 400 Ar les 100 g."""
        prix = prix_par_unite_de_reference(
            Decimal("4000"), Decimal("1"), "kg", ReferenceUnit.G
        )
        assert prix == Decimal("400")

    def test_unite_locale_avec_facteur_mesure(self):
        """1 kapoaka (285 g) à 3000 Ar → ~1052 Ar les 100 g."""
        conversions = [IngredientConversion("kapoaka", "g", Decimal("285"))]
        prix = prix_par_unite_de_reference(
            Decimal("3000"),
            Decimal("1"),
            "kapoaka",
            ReferenceUnit.G,
            conversions=conversions,
        )
        assert prix.quantize(Decimal("0.01")) == Decimal("1052.63")

    def test_facteur_manquant_bloque(self):
        """Le cœur de la règle : un kapoaka converti « au jugé » produirait un
        budget faux et crédible, pire qu'un budget absent."""
        with pytest.raises(UnitConversionError):
            prix_par_unite_de_reference(
                Decimal("3000"), Decimal("1"), "kapoaka", ReferenceUnit.G
            )

    def test_densite_manquante_bloque_poids_vers_volume(self):
        with pytest.raises(UnitConversionError):
            prix_par_unite_de_reference(
                Decimal("5000"), Decimal("1"), "l", ReferenceUnit.G
            )


class TestAppariement:
    INDEX = {"riz blanc (cru)": "riz-blanc-cru", "vary": "riz-blanc-cru", "oignon": "oignon"}

    def test_appariement_exact(self):
        assert apparier_ingredient("Riz blanc (cru)", self.INDEX) == "riz-blanc-cru"

    def test_insensible_a_la_casse_et_aux_espaces(self):
        assert apparier_ingredient("  OIGNON  ", self.INDEX) == "oignon"

    def test_insensible_aux_accents(self):
        index = {"brede mafana": "brede-mafana"}
        assert apparier_ingredient("Brède Mafana", index) == "brede-mafana"

    def test_alias_local(self):
        assert apparier_ingredient("Vary", self.INDEX) == "riz-blanc-cru"

    def test_aucun_appariement_flou(self):
        """« riz rouge » ne doit pas être rapproché de « riz blanc » : la ligne
        part en revue manuelle plutôt qu'en devinette."""
        assert apparier_ingredient("riz rouge", self.INDEX) is None
        assert apparier_ingredient("riz", self.INDEX) is None


class TestAberration:
    def test_sans_mediane_connue_rien_n_est_aberrant(self):
        """Le premier prix d'un ingrédient n'a rien à quoi se comparer."""
        assert est_aberrant(Decimal("99999"), None) is False

    def test_ecart_superieur_a_50_pourcent(self):
        assert est_aberrant(Decimal("1600"), Decimal("1000")) is True
        assert est_aberrant(Decimal("400"), Decimal("1000")) is True

    def test_ecart_dans_la_tolerance(self):
        assert est_aberrant(Decimal("1400"), Decimal("1000")) is False
        assert est_aberrant(Decimal("1000"), Decimal("1000")) is False

    def test_mediane_nulle_ne_divise_pas_par_zero(self):
        assert est_aberrant(Decimal("500"), Decimal("0")) is False
