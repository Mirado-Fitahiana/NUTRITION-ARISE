"""Conversion des quantités (FN-012).

Règle directrice : **une conversion absente bloque et le signale, elle n'invente
jamais un facteur.** Une quantité fausse produit une valeur nutritionnelle
fausse puis une liste de courses fausse — et la confiance ne revient pas.

Trois niveaux, du plus général au plus spécifique :

1. conversions dimensionnelles universelles (kg → g, l → ml) ;
2. conversion masse ↔ volume, qui exige la **densité de l'ingrédient** ;
3. conversions locales mesurées, propres à un ingrédient (`kapoaka` de riz,
   botte de brèdes, tas de tomates), lues dans `ingredient_unit_conversions`.

Aucune formule générique ne relie une unité locale à une masse : seule la mesure
fait foi.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import ReferenceUnit

#: Facteurs vers l'unité de base de chaque dimension (g pour la masse, ml pour
#: le volume). Universels : ils ne dépendent pas de l'ingrédient.
MASS_TO_G: dict[str, Decimal] = {
    "mg": Decimal("0.001"),
    "g": Decimal("1"),
    "kg": Decimal("1000"),
}

VOLUME_TO_ML: dict[str, Decimal] = {
    "ml": Decimal("1"),
    "cl": Decimal("10"),
    "dl": Decimal("100"),
    "l": Decimal("1000"),
}

#: Unités de dénombrement : une pièce, sans conversion dimensionnelle possible.
COUNT_UNITS: frozenset[str] = frozenset({"unit", "piece", "unite"})


class UnitConversionError(Exception):
    """Conversion impossible. Doit remonter jusqu'à l'utilisateur ou bloquer la
    publication — jamais être absorbée par une valeur par défaut."""

    def __init__(self, from_unit: str, to_unit: str, reason: str) -> None:
        self.from_unit = from_unit
        self.to_unit = to_unit
        self.reason = reason
        super().__init__(f"conversion {from_unit} → {to_unit} impossible : {reason}")


@dataclass(frozen=True)
class IngredientConversion:
    """Facteur mesuré pour un ingrédient donné."""

    from_unit: str
    to_unit: str
    factor: Decimal


def normalize_unit(unit: str) -> str:
    return unit.strip().lower()


def _dimension(unit: str) -> str | None:
    if unit in MASS_TO_G:
        return "mass"
    if unit in VOLUME_TO_ML:
        return "volume"
    if unit in COUNT_UNITS:
        return "count"
    return None


def to_reference_quantity(
    quantity: Decimal,
    unit: str,
    reference_unit: ReferenceUnit,
    *,
    density_g_per_ml: Decimal | None = None,
    conversions: list[IngredientConversion] | None = None,
) -> Decimal:
    """Exprime `quantity unit` dans l'unité de référence de l'ingrédient.

    L'unité de référence est celle des valeurs nutritionnelles pour 100
    (`g`, `ml`, ou `unit`).

    Lève `UnitConversionError` dès qu'un facteur manque.
    """
    unit = normalize_unit(unit)
    target = reference_unit.value
    conversions = conversions or []

    if quantity <= 0:
        raise UnitConversionError(unit, target, "quantité non strictement positive")

    if unit == target:
        return quantity

    # 1. Conversion locale mesurée — prioritaire : elle est propre à
    #    l'ingrédient et l'emporte sur toute règle générale.
    for conversion in conversions:
        if (
            normalize_unit(conversion.from_unit) == unit
            and normalize_unit(conversion.to_unit) == target
        ):
            return quantity * conversion.factor

    source_dim = _dimension(unit)
    target_dim = _dimension(target)

    if source_dim is None:
        raise UnitConversionError(
            unit,
            target,
            "unité inconnue et aucune conversion mesurée pour cet ingrédient",
        )

    # 2. Même dimension : conversion universelle.
    if source_dim == target_dim == "mass":
        return quantity * MASS_TO_G[unit] / MASS_TO_G[target]
    if source_dim == target_dim == "volume":
        return quantity * VOLUME_TO_ML[unit] / VOLUME_TO_ML[target]

    # 3. Masse ↔ volume : exige la densité de l'ingrédient.
    if source_dim == "volume" and target_dim == "mass":
        if density_g_per_ml is None:
            raise UnitConversionError(unit, target, "densité de l'ingrédient inconnue")
        millilitres = quantity * VOLUME_TO_ML[unit]
        return millilitres * density_g_per_ml / MASS_TO_G[target]

    if source_dim == "mass" and target_dim == "volume":
        if density_g_per_ml is None or density_g_per_ml == 0:
            raise UnitConversionError(unit, target, "densité de l'ingrédient inconnue")
        grams = quantity * MASS_TO_G[unit]
        return grams / density_g_per_ml / VOLUME_TO_ML[target]

    # 4. Dénombrement : aucune règle générale ne relie une pièce à une masse.
    raise UnitConversionError(
        unit,
        target,
        "conversion entre dénombrement et grandeur physique — une mesure par "
        "ingrédient est obligatoire",
    )


__all__ = [
    "COUNT_UNITS",
    "IngredientConversion",
    "MASS_TO_G",
    "VOLUME_TO_ML",
    "UnitConversionError",
    "normalize_unit",
    "to_reference_quantity",
]
