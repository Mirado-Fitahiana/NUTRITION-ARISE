"""Déroulé d'une collecte — FN-013, avec les verrous du plan (§5.1).

Pur au sens du reste du service : ni SQLAlchemy ni FastAPI. Le réseau arrive par
un client `httpx` injecté, le temps par une fonction d'attente injectée, la
persistance par un `Rapporteur`. C'est ce qui permet de vérifier **sans réseau**
que le délai plancher est respecté, qu'un `robots.txt` interdisant arrête la
page, qu'une redirection hors du site est refusée — et qu'aucun prix n'est écrit :
ce module ne connaît tout simplement pas `ingredient_prices`.

Deux modes :

* `structure` — l'empreinte de SPIKE-01, critère 3 : une seule page, on compte
  les produits et les prix affichés, aucune offre n'est conservée ;
* `collecte` — la collecte **à blanc** : pages configurées, lecture des prix,
  appariement exact, offres conservées en transit pour inspection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from app.services.collecte.connecteurs import connecteur_pour
from app.services.collecte.extraction import (
    PRIX_ILLISIBLE,
    PRIX_LU,
    lire_conditionnement,
    lire_prix,
    nettoyer_texte,
    taux_echec,
)
from app.services.collecte.politesse import (
    TAILLE_MAX_OCTETS,
    contact_ascii,
    delai_effectif,
    interpreter_robots,
    meme_site,
    pages_effectives,
    url_robots,
    user_agent,
)
from app.services.pricing import apparier_ingredient, normaliser_libelle

MODE_STRUCTURE = "structure"
MODE_COLLECTE = "collecte"
MODES = (MODE_STRUCTURE, MODE_COLLECTE)

#: FN-013 — au-delà, une alerte de changement de structure est levée.
SEUIL_ALERTE_PAR_DEFAUT = Decimal("0.20")

ETAPES = ("preparation", "robots", "telechargement", "extraction", "appariement", "rapport")


@dataclass(frozen=True)
class SourceCollecte:
    slug: str
    nom: str
    plateforme: str
    base_url: str
    pages: tuple[str, ...]
    delai_s: Decimal
    max_pages: int


@dataclass(frozen=True)
class ParametresCollecte:
    mode: str
    contact: str = ""
    delai_reglage_s: Decimal | None = None
    max_pages_reglage: int | None = None
    seuil_alerte: Decimal = SEUIL_ALERTE_PAR_DEFAUT
    #: Sous-ensemble des pages de la source, choisi à l'écran.
    pages: tuple[str, ...] | None = None


@dataclass(frozen=True)
class OffreEnregistree:
    """Une offre prête à être conservée en transit. Jamais un prix observé."""

    url: str | None
    libelle: str
    libelle_normalise: str
    prix_texte: str | None
    prix: Decimal | None
    devise: str | None
    statut_prix: str
    motif_prix: str | None
    conditionnement: str | None
    quantite: Decimal | None
    unite: str | None
    disponibilite: str | None
    ingredient_slug: str | None
    statut_appariement: str
    motif_appariement: str


@dataclass
class Bilan:
    statut: str  # succeeded | failed | cancelled
    compteurs: dict[str, Any]
    alerte: bool = False
    code_erreur: str | None = None
    detail_erreur: str | None = None


class Rapporteur(Protocol):
    """Ce que le moteur dit de son avancement. L'implémentation SQL vit dans
    `app.services.executions` ; les tests en utilisent une en mémoire."""

    async def evenement(
        self, niveau: str, etape: str | None, message: str, donnees: dict | None = None
    ) -> None: ...

    async def progression(self, etape: str, fait: int, total: int | None) -> None: ...

    async def compteurs(self, compteurs: dict[str, Any]) -> None: ...

    async def offres(self, offres: list[OffreEnregistree]) -> None: ...

    async def erreur(
        self,
        etape: str,
        message: str,
        *,
        url: str | None = None,
        http_status: int | None = None,
        contexte: dict | None = None,
    ) -> None: ...

    async def instantane(self, index: int, url: str, html: str) -> None: ...

    async def annulation_demandee(self) -> bool: ...


Attente = Callable[[float], Awaitable[Any]]

#: Redirections suivies au plus pour une même page.
MAX_REDIRECTIONS = 3


class RedirectionRefusee(Exception):
    """Redirection hors du site déclaré, vers une page non autorisée, ou chaîne
    trop longue. Levée **avant** toute requête vers la destination."""


def creer_client(
    contact: str | None, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Client HTTP de la collecte : agent ARISE, délai de réponse borné, et
    **aucune redirection suivie automatiquement**. Suivie par `httpx`, une
    redirection vers un autre site aurait déjà émis la requête au moment où on
    la refuserait ; ici, chacune est vérifiée avant d'être suivie."""
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(contact)},
        timeout=httpx.Timeout(30.0),
        follow_redirects=False,
        transport=transport,
    )


async def _telecharger(
    client: httpx.AsyncClient,
    url: str,
    base_url: str,
    refus: Callable[[str], str | None] | None = None,
) -> httpx.Response:
    courant = url
    for _ in range(MAX_REDIRECTIONS + 1):
        reponse = await client.get(courant)
        if not reponse.is_redirect:
            return reponse
        destination = urljoin(courant, reponse.headers.get("location", ""))
        if not meme_site(destination, base_url):
            raise RedirectionRefusee(
                f"redirection hors du site déclaré vers {destination[:200]} — non suivie"
            )
        motif = refus(destination) if refus else None
        if motif:
            raise RedirectionRefusee(f"redirection vers une page non autorisée — {motif}")
        courant = destination
    raise RedirectionRefusee(f"plus de {MAX_REDIRECTIONS} redirections — abandon")


def _refus_robots(statut: int | None, texte: str | None, url: str) -> str | None:
    verdict = interpreter_robots(statut, texte, url)
    return None if verdict.autorise is True else verdict.motif


def _compteurs_initiaux(mode: str, total: int) -> dict[str, Any]:
    compteurs: dict[str, Any] = {
        "mode": mode,
        "pages_total": total,
        "pages_lues": 0,
        "pages_ignorees": 0,
        "octets": 0,
        "http_status": None,
        "robots_autorise": None,
        # Noms repris de `spike01/releves.jsonl`, pour que l'archive et les
        # relevés de la plateforme se comparent directement.
        "produits": 0,
        "prix_trouves": 0,
        "exemples_prix": [],
        "taille_octets": 0,
        "prix_lus": 0,
        "prix_illisibles": 0,
        "erreurs": 0,
    }
    if mode == MODE_COLLECTE:
        compteurs.update(
            {"offres": 0, "appariees": 0, "non_appariees": 0, "ingredients_apparies": 0}
        )
    return compteurs


async def _analyser(
    html: str,
    url: str,
    source: SourceCollecte,
    mode: str,
    rapporteur: Rapporteur,
    index: dict[str, str],
    compteurs: dict[str, Any],
    apparies: set[str],
) -> int:
    """Extraction, lecture et appariement d'une page. Rend le nombre de blocs
    produit reconnus."""
    resultat = connecteur_pour(source.plateforme).extraire(html, url)
    compteurs["produits"] += resultat.blocs_produit

    offres: list[OffreEnregistree] = []
    for brute in resultat.offres:
        prix = lire_prix(brute.prix_texte)
        if brute.prix_texte is not None:
            compteurs["prix_trouves"] += 1
            if len(compteurs["exemples_prix"]) < 3:
                compteurs["exemples_prix"].append(nettoyer_texte(brute.prix_texte)[:40])
        if prix.statut == PRIX_LU:
            compteurs["prix_lus"] += 1
        elif prix.statut == PRIX_ILLISIBLE:
            compteurs["prix_illisibles"] += 1

        if mode != MODE_COLLECTE:
            continue

        conditionnement = lire_conditionnement(brute.libelle)
        slug = apparier_ingredient(brute.libelle, index)
        if slug:
            apparies.add(slug)
        offres.append(
            OffreEnregistree(
                url=brute.url,
                libelle=brute.libelle[:500],
                libelle_normalise=normaliser_libelle(brute.libelle)[:500],
                prix_texte=nettoyer_texte(brute.prix_texte)[:120] if brute.prix_texte else None,
                prix=prix.montant,
                devise=prix.devise,
                statut_prix=prix.statut,
                motif_prix=prix.motif,
                conditionnement=conditionnement.brut,
                quantite=conditionnement.quantite,
                unite=conditionnement.unite,
                disponibilite=brute.disponibilite,
                ingredient_slug=slug,
                statut_appariement="matched" if slug else "unmatched",
                motif_appariement=(
                    "appariement exact (slug, nom ou alias)"
                    if slug
                    else "aucun ingrédient ni alias ne porte ce libellé"
                ),
            )
        )

    if mode == MODE_COLLECTE and offres:
        await rapporteur.progression("appariement", compteurs["pages_lues"], compteurs["pages_total"])
        await rapporteur.offres(offres)
        compteurs["offres"] += len(offres)
        compteurs["appariees"] += sum(1 for o in offres if o.ingredient_slug)
        compteurs["non_appariees"] += sum(1 for o in offres if not o.ingredient_slug)
        compteurs["ingredients_apparies"] = len(apparies)

    if resultat.blocs_produit == 0:
        await rapporteur.evenement(
            "warning",
            "extraction",
            "Aucun bloc produit reconnu sur cette page : la structure a peut-être changé.",
            {"url": url},
        )
    else:
        await rapporteur.evenement(
            "info",
            "extraction",
            f"{resultat.blocs_produit} produits · {len(resultat.offres)} libellés · "
            f"{sum(1 for o in resultat.offres if o.prix_texte)} prix affichés",
            {"url": url},
        )
    return resultat.blocs_produit


async def _conclure(
    compteurs: dict[str, Any],
    params: ParametresCollecte,
    rapporteur: Rapporteur,
    pages_sans_produit: int,
) -> Bilan:
    taux = taux_echec(compteurs["prix_lus"], compteurs["prix_illisibles"])
    compteurs["taux_echec_extraction"] = float(taux) if taux is not None else None

    alerte = False
    if taux is not None and taux > params.seuil_alerte:
        alerte = True
        await rapporteur.evenement(
            "warning",
            "rapport",
            f"Alerte de structure : {taux:.0%} des prix affichés sont illisibles "
            f"(seuil {params.seuil_alerte:.0%}, FN-013).",
        )
    if pages_sans_produit:
        alerte = True

    await rapporteur.progression("rapport", compteurs["pages_lues"], compteurs["pages_total"])
    await rapporteur.compteurs(compteurs)

    if compteurs["pages_lues"] == 0:
        return Bilan(
            "failed",
            compteurs,
            alerte,
            "AUCUNE_PAGE_LUE",
            "Aucune page n'a pu être relevée : voir les erreurs de l'exécution.",
        )

    await rapporteur.evenement(
        "info",
        "rapport",
        f"Terminé : {compteurs['pages_lues']} page(s) lue(s), {compteurs['produits']} produits, "
        f"{compteurs['prix_lus']} prix lus."
        + (
            f" {compteurs['appariees']} offre(s) appariée(s), "
            f"{compteurs['non_appariees']} à revoir."
            if params.mode == MODE_COLLECTE
            else ""
        ),
    )
    return Bilan("succeeded", compteurs, alerte)


async def executer(
    source: SourceCollecte,
    params: ParametresCollecte,
    *,
    client: httpx.AsyncClient,
    rapporteur: Rapporteur,
    index: dict[str, str],
    attendre: Attente = asyncio.sleep,
) -> Bilan:
    """Une collecte complète. Ne lève pas : tout échec finit dans le bilan."""
    if params.mode not in MODES:
        raise ValueError(f"mode inconnu : {params.mode}")

    demandees = params.pages or source.pages
    if params.mode == MODE_STRUCTURE:
        # « Une page par source et par passage » : on mesure une structure.
        pages = list(demandees[:1])
    else:
        pages = pages_effectives(demandees, source.max_pages, params.max_pages_reglage)
    delai = delai_effectif(source.delai_s, params.delai_reglage_s)
    compteurs = _compteurs_initiaux(params.mode, len(pages))

    await rapporteur.progression("preparation", 0, len(pages))
    await rapporteur.evenement(
        "info",
        "preparation",
        f"Agent annoncé : {user_agent(params.contact)}",
        {"delai_s": float(delai), "pages": len(pages)},
    )
    if not contact_ascii(params.contact):
        await rapporteur.evenement(
            "warning",
            "preparation",
            "Coordonnées de contact non renseignées (SCRAPING_CONTACT) : FN-013 les exige.",
        )

    if not pages:
        await rapporteur.compteurs(compteurs)
        return Bilan("failed", compteurs, False, "AUCUNE_PAGE", "La source n'a aucune page configurée.")

    # --- robots.txt, relu à chaque exécution --------------------------------
    await rapporteur.progression("robots", 0, len(pages))
    statut_robots: int | None
    texte_robots: str | None
    # Le robots.txt du site déclaré par la source — jamais celui d'une page
    # mal configurée qui pointerait ailleurs.
    adresse_robots = url_robots(source.base_url)
    try:
        reponse = await _telecharger(client, adresse_robots, source.base_url)
        statut_robots, texte_robots = reponse.status_code, reponse.text
    except (httpx.HTTPError, RedirectionRefusee) as exc:
        statut_robots, texte_robots = None, None
        await rapporteur.erreur(
            "robots", f"robots.txt injoignable : {type(exc).__name__}", url=adresse_robots
        )

    autorisees: list[str] = []
    for page in pages:
        if not meme_site(page, source.base_url):
            compteurs["pages_ignorees"] += 1
            compteurs["erreurs"] += 1
            await rapporteur.erreur("preparation", "page hors du site déclaré par la source", url=page)
            continue
        verdict = interpreter_robots(statut_robots, texte_robots, page)
        if compteurs["robots_autorise"] is None or verdict.autorise is not True:
            compteurs["robots_autorise"] = verdict.autorise
        if verdict.autorise is True:
            autorisees.append(page)
            continue
        compteurs["pages_ignorees"] += 1
        await rapporteur.evenement("warning", "robots", f"Page ignorée — {verdict.motif}", {"url": page})

    await rapporteur.compteurs(compteurs)
    if not autorisees:
        refus = compteurs["robots_autorise"] is False
        return Bilan(
            "failed",
            compteurs,
            False,
            "ROBOTS_REFUS" if refus else "ROBOTS_INCERTAIN",
            "robots.txt interdit les pages demandées."
            if refus
            else "Aucune page autorisée avec certitude : rien n'a été relevé.",
        )

    # --- pages ----------------------------------------------------------------
    apparies: set[str] = set()
    pages_sans_produit = 0
    for numero, page in enumerate(autorisees):
        if await rapporteur.annulation_demandee():
            await rapporteur.evenement("warning", None, "Annulation demandée : arrêt avant la page suivante.")
            await rapporteur.compteurs(compteurs)
            return Bilan("cancelled", compteurs, False)

        if numero:
            await rapporteur.progression("attente", compteurs["pages_lues"], len(pages))
            await attendre(float(delai))

        await rapporteur.progression("telechargement", compteurs["pages_lues"], len(pages))
        try:
            reponse = await _telecharger(
                client,
                page,
                source.base_url,
                refus=lambda url: _refus_robots(statut_robots, texte_robots, url),
            )
        except RedirectionRefusee as exc:
            compteurs["erreurs"] += 1
            await rapporteur.erreur("telechargement", str(exc), url=page)
            continue
        except httpx.HTTPError as exc:
            compteurs["erreurs"] += 1
            await rapporteur.erreur("telechargement", f"{type(exc).__name__}", url=page)
            continue

        compteurs["http_status"] = reponse.status_code
        if reponse.status_code != 200:
            compteurs["erreurs"] += 1
            await rapporteur.erreur(
                "telechargement", f"HTTP {reponse.status_code}", url=page, http_status=reponse.status_code
            )
            continue
        if len(reponse.content) > TAILLE_MAX_OCTETS:
            compteurs["erreurs"] += 1
            await rapporteur.erreur("telechargement", "page trop volumineuse", url=page)
            continue

        html = reponse.text
        compteurs["pages_lues"] += 1
        compteurs["octets"] += len(reponse.content)
        compteurs["taille_octets"] = compteurs["octets"]
        await rapporteur.instantane(numero, page, html)

        await rapporteur.progression("extraction", compteurs["pages_lues"], len(pages))
        blocs = await _analyser(html, page, source, params.mode, rapporteur, index, compteurs, apparies)
        if blocs == 0:
            pages_sans_produit += 1
        await rapporteur.compteurs(compteurs)

    return await _conclure(compteurs, params, rapporteur, pages_sans_produit)


async def rejouer(
    source: SourceCollecte,
    instantanes: Sequence[tuple[str, str]],
    params: ParametresCollecte,
    *,
    rapporteur: Rapporteur,
    index: dict[str, str],
) -> Bilan:
    """Ré-extraction sur des instantanés : **aucune requête au site**.

    Sert après la correction d'un connecteur, ou pour réapparier des libellés
    après l'ajout d'alias.
    """
    compteurs = _compteurs_initiaux(MODE_COLLECTE, len(instantanes))
    compteurs["mode"] = "rejeu"
    await rapporteur.evenement(
        "info", "preparation", f"Rejeu sur {len(instantanes)} instantané(s) — aucune requête au site."
    )

    apparies: set[str] = set()
    pages_sans_produit = 0
    for url, html in instantanes:
        if await rapporteur.annulation_demandee():
            await rapporteur.compteurs(compteurs)
            return Bilan("cancelled", compteurs, False)
        compteurs["pages_lues"] += 1
        compteurs["octets"] += len(html.encode("utf-8"))
        await rapporteur.progression("extraction", compteurs["pages_lues"], len(instantanes))
        blocs = await _analyser(html, url, source, MODE_COLLECTE, rapporteur, index, compteurs, apparies)
        if blocs == 0:
            pages_sans_produit += 1
        await rapporteur.compteurs(compteurs)

    return await _conclure(compteurs, params, rapporteur, pages_sans_produit)


@dataclass
class RapporteurMemoire:
    """Rapporteur en mémoire — pour les tests, et pour comprendre le contrat."""

    evenements: list[tuple[str, str | None, str]] = field(default_factory=list)
    etapes: list[tuple[str, int, int | None]] = field(default_factory=list)
    derniers_compteurs: dict[str, Any] = field(default_factory=dict)
    offres_recues: list[OffreEnregistree] = field(default_factory=list)
    erreurs: list[dict[str, Any]] = field(default_factory=list)
    instantanes: list[tuple[int, str]] = field(default_factory=list)
    annuler_apres: int | None = None
    _verifications: int = 0

    async def evenement(self, niveau, etape, message, donnees=None) -> None:
        self.evenements.append((niveau, etape, message))

    async def progression(self, etape, fait, total) -> None:
        self.etapes.append((etape, fait, total))

    async def compteurs(self, compteurs) -> None:
        self.derniers_compteurs = dict(compteurs)

    async def offres(self, offres) -> None:
        self.offres_recues.extend(offres)

    async def erreur(self, etape, message, *, url=None, http_status=None, contexte=None) -> None:
        self.erreurs.append(
            {"etape": etape, "message": message, "url": url, "http_status": http_status}
        )

    async def instantane(self, index, url, html) -> None:
        self.instantanes.append((index, url))

    async def annulation_demandee(self) -> bool:
        self._verifications += 1
        return self.annuler_apres is not None and self._verifications > self.annuler_apres


__all__ = [
    "Bilan",
    "ETAPES",
    "MODES",
    "MODE_COLLECTE",
    "MODE_STRUCTURE",
    "OffreEnregistree",
    "ParametresCollecte",
    "Rapporteur",
    "RapporteurMemoire",
    "RedirectionRefusee",
    "SEUIL_ALERTE_PAR_DEFAUT",
    "SourceCollecte",
    "creer_client",
    "executer",
    "rejouer",
]
