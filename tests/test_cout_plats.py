"""Coût des plats (FN-017, sprint 06).

Ce que ces tests tiennent tient en une phrase : **un coût partiel ne doit
jamais se présenter comme un coût complet.** Sur un marché informel où les prix
bougent vite, la crédibilité du produit repose sur l'honnêteté de l'estimation,
pas sur son apparente précision.

Trois classes de bugs sont couvertes, toutes silencieuses :

* un ingrédient sans prix **omis** du total au lieu d'être signalé — le total
  paraît complet et vaut moins que le panier réel ;
* un prix extrapolé depuis un autre ingrédient ;
* un périmètre mélangé — un prix de Tananarive additionné à un indice national
  donne un total qui ne correspond à aucun panier réel.

Aucune base n'est nécessaire.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.enums import PriceFreshness, PriceSource, ReferenceUnit
from app.services.pricing import (
    SEUIL_COUVERTURE_CHIFFRABLE,
    CoutIngredient,
    CoutStatut,
    ReleveCandidat,
    choisir_perimetre,
    cout_ingredient,
    cout_plat,
)
from app.services.units import IngredientConversion

MAINTENANT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
KAPOAKA = [IngredientConversion("kapoaka", "g", Decimal("285"))]


def releve(
    prix: str,
    *,
    vendeur: str = "marche-analakely",
    zone: str = "Antananarivo",
    unite: str = "kg",
    quantite: str = "1",
    jours: int = 0,
    source: PriceSource = PriceSource.MANUAL,
) -> ReleveCandidat:
    return ReleveCandidat(
        vendor_slug=vendeur,
        vendor_zone=zone,
        price=Decimal(prix),
        unit=unite,
        quantity=Decimal(quantite),
        collected_at=MAINTENANT - timedelta(days=jours),
        source=source,
    )


def ligne_chiffree(slug: str, cout: str) -> CoutIngredient:
    from app.services.pricing import PerimetreRetenu

    return CoutIngredient(
        ingredient_slug=slug,
        cout_min=Decimal(cout),
        cout_median=Decimal(cout),
        cout_max=Decimal(cout),
        retenu=PerimetreRetenu(
            portee="vendor",
            vendor_slug="marche-analakely",
            observed_at=MAINTENANT,
            source=PriceSource.MANUAL,
            observations=1,
        ),
        freshness=PriceFreshness.RECENT,
    )


class TestChoixDuPerimetre:
    def test_le_vendeur_demande_prime(self):
        candidats = [
            releve("4000", vendeur="marche-analakely"),
            releve("4500", vendeur="kibo-tananarive"),
        ]
        retenus = choisir_perimetre(
            candidats, vendeur_prefere="kibo-tananarive", maintenant=MAINTENANT
        )

        assert [c.vendor_slug for c in retenus] == ["kibo-tananarive"]

    def test_repli_sur_la_zone_si_le_vendeur_n_a_rien(self):
        candidats = [releve("4000", vendeur="marche-analakely", zone="Antananarivo")]
        retenus = choisir_perimetre(
            candidats,
            vendeur_prefere="kibo-tananarive",
            zone="Antananarivo",
            maintenant=MAINTENANT,
        )

        assert [c.vendor_slug for c in retenus] == ["marche-analakely"]

    def test_repli_final_sur_l_officiel(self):
        candidats = [releve("3400", vendeur="instat", source=PriceSource.OFFICIAL)]
        retenus = choisir_perimetre(
            candidats, vendeur_prefere="kibo-tananarive", zone="Toamasina", maintenant=MAINTENANT
        )

        assert len(retenus) == 1
        assert retenus[0].source == PriceSource.OFFICIAL

    def test_les_perimetres_ne_se_melangent_jamais(self):
        """Additionner un prix de Tananarive et un indice national donnerait un
        total qui ne correspond à aucun panier réel."""
        candidats = [
            releve("4000", vendeur="marche-analakely", zone="Antananarivo"),
            releve("3400", vendeur="instat", source=PriceSource.OFFICIAL),
        ]
        retenus = choisir_perimetre(candidats, zone="Antananarivo", maintenant=MAINTENANT)

        assert all(c.source == PriceSource.MANUAL for c in retenus)

    def test_les_obsoletes_sont_ecartees(self):
        candidats = [releve("4000", jours=100)]
        assert choisir_perimetre(candidats, maintenant=MAINTENANT) == []


class TestCoutIngredient:
    def test_conversion_directe(self):
        """200 g de tomate à 2500 Ar/kg → 500 Ar."""
        ligne = cout_ingredient(
            "tomate",
            Decimal("200"),
            "g",
            ReferenceUnit.G,
            [releve("2500", unite="kg")],
            maintenant=MAINTENANT,
        )

        assert ligne.chiffre is True
        assert ligne.cout_median == Decimal("500.00")
        assert ligne.freshness == PriceFreshness.RECENT

    def test_unite_locale_des_deux_cotes(self):
        """300 g de riz, relevé à 3000 Ar le kapoaka (285 g) → ~3157 Ar."""
        ligne = cout_ingredient(
            "riz-blanc-cru",
            Decimal("300"),
            "g",
            ReferenceUnit.G,
            [releve("3000", unite="kapoaka")],
            conversions=KAPOAKA,
            maintenant=MAINTENANT,
        )

        assert ligne.cout_median == Decimal("3157.89")

    def test_sans_releve_la_ligne_porte_son_motif(self):
        ligne = cout_ingredient(
            "brede-mafana", Decimal("250"), "g", ReferenceUnit.G, [], maintenant=MAINTENANT
        )

        assert ligne.chiffre is False
        assert ligne.motif == "aucun relevé de prix"
        assert ligne.cout_median is None

    def test_tous_releves_obsoletes(self):
        ligne = cout_ingredient(
            "oignon",
            Decimal("100"),
            "g",
            ReferenceUnit.G,
            [releve("4000", jours=100)],
            maintenant=MAINTENANT,
        )

        assert ligne.chiffre is False
        assert ligne.motif == "tous les relevés sont obsolètes"

    def test_conversion_manquante_ne_chiffre_pas(self):
        """`units.py` bloque, et la ligne devient non chiffrable — elle n'est
        pas estimée au jugé."""
        ligne = cout_ingredient(
            "riz-blanc-cru",
            Decimal("1"),
            "kapoaka",
            ReferenceUnit.G,
            [releve("3000", unite="kg")],
            maintenant=MAINTENANT,  # aucune conversion fournie
        )

        assert ligne.chiffre is False
        assert "non convertible" in ligne.motif

    def test_releve_en_unite_incompatible_est_ignore_pas_fatal(self):
        """Un vendeur relève au kapoaka sans facteur connu, un autre au kilo :
        le second suffit."""
        ligne = cout_ingredient(
            "riz-blanc-cru",
            Decimal("300"),
            "g",
            ReferenceUnit.G,
            [
                releve("3000", unite="kapoaka", vendeur="marche-analakely"),
                releve("9000", unite="kg", vendeur="kibo-tananarive"),
            ],
            maintenant=MAINTENANT,
        )

        assert ligne.chiffre is True
        assert ligne.cout_median == Decimal("2700.00")

    def test_tracabilite_du_perimetre(self):
        ligne = cout_ingredient(
            "tomate",
            Decimal("100"),
            "g",
            ReferenceUnit.G,
            [releve("2500", unite="kg", vendeur="kibo-tananarive")],
            vendeur_prefere="kibo-tananarive",
            maintenant=MAINTENANT,
        )

        assert ligne.retenu is not None
        assert ligne.retenu.portee == "vendor"
        assert ligne.retenu.vendor_slug == "kibo-tananarive"
        assert ligne.retenu.observed_at == MAINTENANT


class TestCoutPlat:
    def test_couverture_complete(self):
        plat = cout_plat(
            [ligne_chiffree("riz", "3000"), ligne_chiffree("tomate", "500")],
            servings=4,
            maintenant=MAINTENANT,
        )

        assert plat.statut == CoutStatut.COMPLET
        assert plat.couverture == Decimal("1.000")
        assert plat.cout_total_median == Decimal("3500.00")
        assert plat.cout_portion_median == Decimal("875.00")

    def test_couverture_partielle_au_dessus_du_seuil(self):
        """3 chiffrés sur 4 = 75 %, au-dessus du seuil : on annonce le coût
        **et** l'ingrédient manquant."""
        lignes = [ligne_chiffree(f"i{i}", "1000") for i in range(3)]
        lignes.append(CoutIngredient(ingredient_slug="manquant", motif="aucun relevé de prix"))

        plat = cout_plat(lignes, servings=1, maintenant=MAINTENANT)

        assert plat.statut == CoutStatut.PARTIEL
        assert plat.couverture == Decimal("0.750")
        assert plat.cout_total_median == Decimal("3000.00")
        assert plat.ingredients_sans_prix == ("manquant",)

    def test_sous_le_seuil_le_total_est_retire(self):
        """Laisser un chiffre à côté d'un avertissement, c'est garantir que le
        chiffre sera lu et l'avertissement ignoré."""
        lignes = [ligne_chiffree("i0", "1000")]
        lignes += [
            CoutIngredient(ingredient_slug=f"m{i}", motif="aucun relevé de prix")
            for i in range(3)
        ]

        plat = cout_plat(lignes, servings=1, maintenant=MAINTENANT)

        assert plat.couverture < SEUIL_COUVERTURE_CHIFFRABLE
        assert plat.statut == CoutStatut.INDISPONIBLE
        assert plat.cout_total_median is None

    def test_aucun_prix_du_tout(self):
        lignes = [
            CoutIngredient(ingredient_slug=f"m{i}", motif="aucun relevé de prix")
            for i in range(3)
        ]

        plat = cout_plat(lignes, servings=2, maintenant=MAINTENANT)

        assert plat.statut == CoutStatut.INDISPONIBLE
        assert plat.couverture == Decimal("0.000")
        assert plat.cout_total_median is None
        assert len(plat.ingredients_sans_prix) == 3

    def test_plat_sans_ingredient(self):
        plat = cout_plat([], servings=1, maintenant=MAINTENANT)

        assert plat.statut == CoutStatut.INDISPONIBLE
        assert plat.couverture == Decimal("0")

    def test_la_fraicheur_suit_l_ingredient_le_plus_perime(self):
        """Un plat n'est pas plus frais que son ingrédient le plus ancien."""
        from app.services.pricing import PerimetreRetenu

        recent = ligne_chiffree("frais", "1000")
        ancien = CoutIngredient(
            ingredient_slug="ancien",
            cout_min=Decimal("500"),
            cout_median=Decimal("500"),
            cout_max=Decimal("500"),
            retenu=PerimetreRetenu(
                portee="vendor",
                vendor_slug="marche-analakely",
                observed_at=MAINTENANT - timedelta(days=30),
                source=PriceSource.MANUAL,
                observations=1,
            ),
            freshness=PriceFreshness.STALE,
        )

        plat = cout_plat([recent, ancien], servings=1, maintenant=MAINTENANT)

        assert plat.freshness == PriceFreshness.STALE
        assert plat.observation_la_plus_ancienne == MAINTENANT - timedelta(days=30)

    def test_aucune_extrapolation_entre_ingredients(self):
        """Le coût du plat est la somme des lignes chiffrées, sans jamais
        déduire le prix d'un ingrédient manquant d'un autre."""
        lignes = [
            ligne_chiffree("a", "1000"),
            ligne_chiffree("b", "1000"),
            ligne_chiffree("c", "1000"),
            CoutIngredient(ingredient_slug="d", motif="aucun relevé de prix"),
        ]

        plat = cout_plat(lignes, servings=1, maintenant=MAINTENANT)

        # 3000 et non 4000 : la quatrième ligne n'est pas estimée à la moyenne.
        assert plat.cout_total_median == Decimal("3000.00")
