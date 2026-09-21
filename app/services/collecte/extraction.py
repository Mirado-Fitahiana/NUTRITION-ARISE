"""Lecture des prix et des conditionnements extraits d'une page (FN-013).

Même doctrine que l'import de fichier (sprint 04) : **rien n'est deviné.** Un
prix sans devise, deux montants dans la même case, un séparateur ambigu — la
valeur reste vide, avec son motif. Un prix faux inséré en silence ne produit pas
d'erreur visible : il produit une liste de courses fausse que personne ne
remarque.

Trois statuts, et un seul compte comme échec d'extraction :

* `read` — un montant en ariary a été lu ;
* `absent` — la page n'affiche aucun prix pour ce produit (« sur demande ») :
  c'est une information, pas un échec ;
* `unreadable` — un prix est affiché mais illisible : **c'est lui** qui alimente
  le taux d'échec de FN-013.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

PRIX_LU = "read"
PRIX_ABSENT = "absent"
PRIX_ILLISIBLE = "unreadable"

_ESPACES = re.compile(r"[\s    ]+")
_NOMBRE = re.compile(r"\d[\d\s  .,]*\d|\d")
_DEVISE_ARIARY = re.compile(r"(?<![a-z])(ar|ariary|mga)(?![a-z])", re.IGNORECASE)
_AUTRE_DEVISE = re.compile(r"[€$£]|(?<![a-z])(eur|usd|euros?)(?![a-z])", re.IGNORECASE)

_UNITES = {
    "kg": "kg",
    "g": "g",
    "gr": "g",
    "grs": "g",
    "l": "l",
    "litre": "l",
    "litres": "l",
    "cl": "cl",
    "ml": "ml",
}
_QUANTITE = r"(\d+(?:[.,]\d+)?)"
_UNITE = r"(kg|grs|gr|g|litres|litre|l|cl|ml)"
_LOT = re.compile(rf"(\d+)\s*[x×]\s*{_QUANTITE}\s*{_UNITE}(?![a-z])", re.IGNORECASE)
_SIMPLE = re.compile(rf"(?<![\d.,]){_QUANTITE}\s*{_UNITE}(?![a-z])", re.IGNORECASE)


@dataclass(frozen=True)
class OffreBrute:
    """Ce qu'un connecteur sait lire d'un bloc produit, sans interprétation."""

    libelle: str
    url: str | None
    #: Texte du prix tel qu'affiché. `None` : aucun prix sur la page.
    prix_texte: str | None
    disponibilite: str | None = None


@dataclass(frozen=True)
class PrixLu:
    montant: Decimal | None
    devise: str | None
    statut: str
    motif: str | None


@dataclass(frozen=True)
class Conditionnement:
    brut: str | None
    quantite: Decimal | None
    unite: str | None


def nettoyer_texte(texte: str | None) -> str:
    """Espaces insécables et fines compris : les pages WooCommerce en sont pleines."""
    return _ESPACES.sub(" ", texte or "").strip()


def _vers_decimal(nombre: str) -> Decimal | None:
    """Interprète un nombre écrit à la française ou à l'anglaise.

    Un séparateur suivi de trois chiffres est un séparateur de milliers ; suivi
    d'un ou deux chiffres, c'est une décimale. Tout autre cas est ambigu.
    """
    brut = _ESPACES.sub("", nombre)
    if "," in brut and "." in brut:
        decimal = "," if brut.rfind(",") > brut.rfind(".") else "."
        milliers = "." if decimal == "," else ","
        brut = brut.replace(milliers, "").replace(decimal, ".")
    elif "," in brut or "." in brut:
        separateur = "," if "," in brut else "."
        morceaux = brut.split(separateur)
        if len(morceaux) == 2 and len(morceaux[1]) in (1, 2):
            brut = f"{morceaux[0]}.{morceaux[1]}"
        elif all(len(m) == 3 for m in morceaux[1:]) and morceaux[0]:
            brut = "".join(morceaux)
        else:
            return None
    try:
        return Decimal(brut).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def lire_prix(texte: str | None) -> PrixLu:
    """Un montant en ariary, ou un refus motivé."""
    propre = nettoyer_texte(texte)
    if texte is None or not propre:
        return PrixLu(None, None, PRIX_ABSENT, "aucun prix affiché")

    if _AUTRE_DEVISE.search(propre):
        return PrixLu(None, None, PRIX_ILLISIBLE, "devise autre que l'ariary")

    nombres = _NOMBRE.findall(propre)
    if not nombres:
        return PrixLu(None, None, PRIX_ILLISIBLE, f"aucun montant dans « {propre[:40]} »")
    if len(nombres) > 1:
        return PrixLu(
            None, None, PRIX_ILLISIBLE, "plusieurs montants (fourchette ou promotion) : ambigu"
        )
    if not _DEVISE_ARIARY.search(propre):
        # Sur un site malgache, un nombre nu est « probablement » en ariary.
        # Probablement ne suffit pas.
        return PrixLu(None, None, PRIX_ILLISIBLE, "devise absente")

    montant = _vers_decimal(nombres[0])
    if montant is None:
        return PrixLu(None, None, PRIX_ILLISIBLE, f"format numérique ambigu : « {nombres[0]} »")
    if montant <= 0:
        return PrixLu(None, None, PRIX_ILLISIBLE, "montant nul")
    return PrixLu(montant, "MGA", PRIX_LU, None)


def _quantite(texte: str) -> Decimal | None:
    try:
        return Decimal(texte.replace(",", "."))
    except InvalidOperation:
        return None


def lire_conditionnement(libelle: str | None) -> Conditionnement:
    """« Sucre blanc 1kg » → 1 kg. Deux conditionnements dans un libellé → rien.

    L'unité est rendue **telle qu'observée** (kg, g, l, cl, ml) : la conversion
    appartient à `app.services.units`, qui bloque si le facteur manque.
    """
    propre = nettoyer_texte(libelle)
    if not propre:
        return Conditionnement(None, None, None)

    lots = _LOT.findall(propre)
    if len(lots) == 1:
        nombre, quantite, unite = lots[0]
        valeur = _quantite(quantite)
        brut = _LOT.search(propre).group(0)  # type: ignore[union-attr]
        if valeur is None:
            return Conditionnement(brut, None, None)
        return Conditionnement(brut, valeur * int(nombre), _UNITES[unite.lower()])

    simples = _SIMPLE.findall(propre)
    distincts = {(q.replace(",", "."), u.lower()) for q, u in simples}
    if len(distincts) != 1:
        brut = " / ".join(sorted({f"{q} {u}" for q, u in simples})) or None
        return Conditionnement(brut, None, None)

    quantite, unite = simples[0]
    return Conditionnement(
        _SIMPLE.search(propre).group(0),  # type: ignore[union-attr]
        _quantite(quantite),
        _UNITES[unite.lower()],
    )


def taux_echec(lus: int, illisibles: int) -> Decimal | None:
    """FN-013 — part des prix affichés qu'on n'a pas su lire.

    Les prix absents n'entrent pas au dénominateur : un produit « sur demande »
    n'est pas un échec d'extraction. `None` quand aucun prix n'était affiché —
    zéro sur zéro n'est pas un taux.
    """
    affiches = lus + illisibles
    if affiches == 0:
        return None
    return (Decimal(illisibles) / Decimal(affiches)).quantize(Decimal("0.001"))


__all__ = [
    "Conditionnement",
    "OffreBrute",
    "PRIX_ABSENT",
    "PRIX_ILLISIBLE",
    "PRIX_LU",
    "PrixLu",
    "lire_conditionnement",
    "lire_prix",
    "nettoyer_texte",
    "taux_echec",
]
