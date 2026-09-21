"""Contrat commun des connecteurs.

Un connecteur est **pur** : il reçoit du HTML et l'URL de la page, il rend des
offres brutes. Pas de réseau, pas de base — c'est ce qui permet de le tester sur
une page enregistrée, et de rejouer l'extraction sur un instantané après une
correction, sans solliciter le site une seconde fois.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from bs4 import Tag

from app.services.collecte.extraction import OffreBrute, nettoyer_texte


@dataclass(frozen=True)
class ResultatExtraction:
    offres: tuple[OffreBrute, ...]
    #: Blocs produit reconnus sur la page. Zéro sur une page de catalogue est le
    #: signe le plus probable d'un changement de structure.
    blocs_produit: int


class Connecteur(Protocol):
    plateforme: str

    def extraire(self, html: str, url_page: str) -> ResultatExtraction: ...


def texte(element: Tag | None) -> str:
    if element is None:
        return ""
    return nettoyer_texte(element.get_text(" ", strip=True))


def lien_absolu(href: object, url_page: str) -> str | None:
    """Seuls les liens `http(s)` sont conservés : un `javascript:` venu d'une
    page tiers finirait sinon dans un attribut `href` de la plateforme."""
    if not isinstance(href, str) or not href.strip():
        return None
    absolu = urljoin(url_page, href.strip())
    return absolu if urlsplit(absolu).scheme in ("http", "https") else None


_REGISTRE: dict[str, Connecteur] = {}


def enregistrer(connecteur: Connecteur) -> Connecteur:
    _REGISTRE[connecteur.plateforme] = connecteur
    return connecteur


def connecteur_pour(plateforme: str) -> Connecteur:
    # Import tardif : les modules de plateforme s'enregistrent en important
    # `base`, et `base` ne doit pas les importer à son chargement.
    from app.services.collecte.connecteurs import prestashop, woocommerce  # noqa: F401

    try:
        return _REGISTRE[plateforme]
    except KeyError as exc:
        raise ValueError(f"aucun connecteur pour la plateforme « {plateforme} »") from exc


def plateformes() -> list[str]:
    from app.services.collecte.connecteurs import prestashop, woocommerce  # noqa: F401

    return sorted(_REGISTRE)


__all__ = [
    "Connecteur",
    "ResultatExtraction",
    "connecteur_pour",
    "enregistrer",
    "lien_absolu",
    "plateformes",
    "texte",
]
