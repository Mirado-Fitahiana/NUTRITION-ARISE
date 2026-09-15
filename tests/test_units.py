"""Conversions d'unités (FN-012).

Le point à protéger n'est pas la conversion réussie, c'est le **refus** de
convertir quand le facteur est inconnu. Une conversion inventée produit une
quantité fausse, donc une valeur nutritionnelle fausse.
"""

from decimal import Decimal

import pytest

from app.models.enums import ReferenceUnit
from app.services.units import (
    IngredientConversion,
    UnitConversionError,
    to_reference_quantity,
)


def q(value: str) -> Decimal:
    return Decimal(value)


class TestConversionsUniverselles:
    def test_meme_unite(self):
        assert to_reference_quantity(q("300"), "g", ReferenceUnit.G) == q("300")

    def test_kg_vers_g(self):
        assert to_reference_quantity(q("1.5"), "kg", ReferenceUnit.G) == q("1500.000")

    def test_litre_vers_ml(self):
        assert to_reference_quantity(q("0.75"), "l", ReferenceUnit.ML) == q("750.00")

    def test_casse_et_espaces_ignores(self):
        assert to_reference_quantity(q("2"), " KG ", ReferenceUnit.G) == q("2000")


class TestMasseVolume:
    def test_volume_vers_masse_avec_densite(self):
        # 20 ml d'huile à 0,915 g/ml = 18,3 g
        result = to_reference_quantity(
            q("20"), "ml", ReferenceUnit.G, density_g_per_ml=q("0.915")
        )
        assert result == q("18.300")

    def test_volume_vers_masse_sans_densite_refuse(self):
        with pytest.raises(UnitConversionError, match="densité"):
            to_reference_quantity(q("20"), "ml", ReferenceUnit.G)

    def test_masse_vers_volume_sans_densite_refuse(self):
        with pytest.raises(UnitConversionError, match="densité"):
            to_reference_quantity(q("100"), "g", ReferenceUnit.ML)


class TestConversionsLocales:
    def test_kapoaka_mesuree(self):
        conversions = [IngredientConversion("kapoaka", "g", q("285"))]
        result = to_reference_quantity(
            q("1"), "kapoaka", ReferenceUnit.G, conversions=conversions
        )
        assert result == q("285")

    def test_unite_locale_inconnue_refusee(self):
        """🔴 Aucune règle générique ne relie une unité locale à une masse."""
        with pytest.raises(UnitConversionError, match="unité inconnue"):
            to_reference_quantity(q("1"), "kapoaka", ReferenceUnit.G)

    def test_conversion_locale_prioritaire_sur_la_densite(self):
        """Une mesure propre à l'ingrédient l'emporte sur toute règle générale."""
        conversions = [IngredientConversion("ml", "g", q("2"))]
        result = to_reference_quantity(
            q("10"),
            "ml",
            ReferenceUnit.G,
            density_g_per_ml=q("0.915"),
            conversions=conversions,
        )
        assert result == q("20")

    def test_denombrement_vers_masse_refuse(self):
        """Un « 1 poulet » n'a pas de masse universelle : il faut une mesure."""
        with pytest.raises(UnitConversionError, match="dénombrement"):
            to_reference_quantity(q("1"), "unit", ReferenceUnit.G)


class TestQuantitesInvalides:
    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_quantite_non_positive_refusee(self, value):
        with pytest.raises(UnitConversionError):
            to_reference_quantity(q(value), "g", ReferenceUnit.G)
