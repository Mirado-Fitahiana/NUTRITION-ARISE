"""Règles de politesse de la collecte (FN-013, plan §5.1 — verrou n° 2).

Ces règles sont **dans le code**, pas dans la configuration : une source mal
réglée ne peut ni descendre sous le délai plancher, ni dépasser le plafond de
pages, ni sortir du site qu'elle déclare. Elles reprennent les garde-fous de
`scripts/spike01_releve.py`, en corrigeant son point faible : le script ne
détectait que `Disallow: /` sous `User-agent: *`, si bien qu'une interdiction
portant sur un chemin précis passait inaperçue. `urllib.robotparser` applique
les règles chemin par chemin.

Fonctions pures : le réseau reste à l'appelant.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from app.models.collecte import DELAI_PLANCHER_S

#: Jeton produit annoncé. Explicite : une collecte qui se cache derrière un faux
#: navigateur n'est pas une collecte qu'on peut défendre.
AGENT_PRODUIT = "ARISE-NutritionBot"
AGENT_VERSION = "0.2"

#: Plafond absolu de pages par exécution, quelle que soit la configuration.
PLAFOND_PAGES = 50

#: Une page de catalogue au-delà de cette taille n'est pas une page de catalogue.
TAILLE_MAX_OCTETS = 5_000_000

DELAI_PLANCHER = Decimal(DELAI_PLANCHER_S)


def contact_ascii(contact: str | None) -> str:
    """Un en-tête HTTP n'accepte que de l'ASCII imprimable. On retire le reste
    plutôt que de laisser `httpx` lever au moment de la requête."""
    if not contact:
        return ""
    propre = "".join(c for c in contact if 32 <= ord(c) < 127)
    return " ".join(propre.split())[:120]


def user_agent(contact: str | None) -> str:
    """User-Agent identifiable, avec coordonnées de contact (FN-013).

    Sans contact configuré, l'agent le dit : « non renseigne ». Il n'invente
    pas d'adresse, et la qualification signale le manque.
    """
    coordonnees = contact_ascii(contact) or "non renseigne"
    return (
        f"{AGENT_PRODUIT}/{AGENT_VERSION} "
        f"(+contact: {coordonnees}; releve de prix ARISE Nutrition)"
    )


def delai_effectif(*delais: Decimal | float | int | None) -> Decimal:
    """Le plus prudent des délais demandés, jamais sous le plancher."""
    valeurs = [Decimal(str(d)) for d in delais if d is not None]
    return max([DELAI_PLANCHER, *valeurs])


def pages_effectives(
    pages: list[str] | tuple[str, ...], *plafonds: int | None
) -> list[str]:
    """Les pages à relever, bornées par le plus strict des plafonds."""
    limite = min([PLAFOND_PAGES, *(p for p in plafonds if p is not None)])
    return list(pages)[: max(0, limite)]


def _hote(url: str) -> str:
    hote = (urlsplit(url).hostname or "").lower()
    return hote[4:] if hote.startswith("www.") else hote


def meme_site(url: str, base_url: str) -> bool:
    """Vrai si `url` appartient au site déclaré par la source.

    Sert deux fois : à la configuration d'une page, et après une redirection —
    un site qui renvoie ailleurs ne nous autorise pas à y aller.
    """
    parties = urlsplit(url)
    if parties.scheme not in ("http", "https"):
        return False
    return bool(_hote(url)) and _hote(url) == _hote(base_url)


def url_robots(url: str) -> str:
    parties = urlsplit(url)
    return f"{parties.scheme}://{parties.netloc}/robots.txt"


@dataclass(frozen=True)
class VerdictRobots:
    """`autorise` vaut `None` quand on ne sait pas : **une incertitude ne vaut
    pas une autorisation**, la page n'est pas relevée."""

    autorise: bool | None
    motif: str


def interpreter_robots(
    statut_http: int | None, texte: str | None, url: str
) -> VerdictRobots:
    """Verdict pour une page, à partir de la réponse à `/robots.txt`.

    Suit la RFC 9309 (§2.3.1.3 et §2.3.1.4) : un `robots.txt` absent (4xx)
    n'impose aucune restriction ; un serveur en erreur (5xx) ou injoignable
    impose de s'abstenir.
    """
    if statut_http is None:
        return VerdictRobots(None, "robots.txt injoignable — la page n'est pas relevée")
    if statut_http >= 500:
        return VerdictRobots(
            None, f"robots.txt en erreur (HTTP {statut_http}) — la page n'est pas relevée"
        )
    if 400 <= statut_http < 500:
        return VerdictRobots(
            True, f"aucun robots.txt (HTTP {statut_http}) : pas de restriction déclarée"
        )
    if not 200 <= statut_http < 300:
        return VerdictRobots(None, f"réponse inattendue à robots.txt (HTTP {statut_http})")

    analyseur = RobotFileParser()
    analyseur.parse((texte or "").splitlines())
    if analyseur.can_fetch(AGENT_PRODUIT, url):
        return VerdictRobots(True, "robots.txt autorise cette page pour notre agent")
    return VerdictRobots(False, "robots.txt interdit cette page à notre agent")


__all__ = [
    "AGENT_PRODUIT",
    "AGENT_VERSION",
    "DELAI_PLANCHER",
    "PLAFOND_PAGES",
    "TAILLE_MAX_OCTETS",
    "VerdictRobots",
    "contact_ascii",
    "delai_effectif",
    "interpreter_robots",
    "meme_site",
    "pages_effectives",
    "url_robots",
    "user_agent",
]
