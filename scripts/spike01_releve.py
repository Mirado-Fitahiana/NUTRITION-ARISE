"""SPIKE-01 — relevé de stabilité des sources de prix (sprint 11, critère 3).

    python scripts/spike01_releve.py                 # un relevé
    python scripts/spike01_releve.py --rapport       # comparer les relevés passés
    python scripts/spike01_releve.py --source kibo   # une seule source

Le Lot 4 est conditionné à un spike qui doit établir qu'au moins **deux
sources** satisfont quatre critères. Trois d'entre eux s'instruisent en une
session ; le troisième — *« la structure est stable sur trois relevés espacés
d'une semaine »* — demande **trois semaines calendaires**. Il est donc
irréductible, et c'est le seul qui ne peut pas être rattrapé en se dépêchant
plus tard.

Ce script sert à commencer à compter maintenant. Il ne scrape rien : il relève
une **empreinte de structure** (le sélecteur de prix produit-il toujours le même
ordre de grandeur de résultats ?) et l'archive. Trois passages hebdomadaires
répondent au critère 3.

Deux garde-fous délibérés :

* **Le `robots.txt` est relu avant chaque relevé**, et une source qui interdit
  l'agent configuré est passée. C'est le critère 4, automatisé plutôt que
  vérifié une fois puis oublié.
* **Une seule page par source et par passage.** Il s'agit de mesurer une
  structure, pas de collecter un catalogue.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
ARCHIVE = RACINE / "spike01" / "releves.jsonl"

#: Agent annoncé. Explicite : une collecte qui se cache derrière un faux
#: navigateur n'est pas une collecte qu'on peut défendre.
AGENT = "ARISE-NutritionBot/0.1 (+contact: équipe ARISE ; relevé de structure SPIKE-01)"

#: Sources pré-qualifiées au sprint 02. `page` est une page de catalogue, pas
#: la page d'accueil : la première passe s'y était arrêtée, et c'est ce qui
#: avait faussé le verdict.
SOURCES: dict[str, dict[str, str]] = {
    "kibo": {
        "nom": "kibo.mg (PrestaShop)",
        "page": "https://www.kibo.mg/tananarive/182-sucres",
        "motif_prix": r'class="[^"]*\bprice\b[^"]*"[^>]*>([^<]{1,40})',
        "motif_produit": r"js-product-miniature",
    },
    "abcie": {
        "nom": "abcie.org (WooCommerce)",
        "page": "https://www.abcie.org/boutique/",
        "motif_prix": r"woocommerce-Price-amount[^>]*>(.*?)</bdi>",
        "motif_produit": r'<li[^>]*class="[^"]*product[^"]*"',
    },
    # bonmarche.mg est volontairement absent : son `robots.txt` interdit
    # nommément plusieurs agents d'IA et porte `Content-Signal: ai-train=no`.
    # Son ajout relève d'un arbitrage de conformité, pas d'une ligne de code
    # (voir sprint-02, « Le point à arbitrer »).
}

DELAI_ENTRE_SOURCES_S = 3.0


@dataclass
class Releve:
    source: str
    horodatage: str
    http_status: int | None
    robots_autorise: bool | None
    produits: int
    prix_trouves: int
    exemples_prix: list[str]
    taille_octets: int
    erreur: str | None = None

    @property
    def exploitable(self) -> bool:
        return self.erreur is None and self.prix_trouves > 0


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _recuperer(url: str, *, timeout: int = 30) -> tuple[int, str]:
    requete = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(requete, timeout=timeout) as reponse:  # noqa: S310
        brut = reponse.read()
        encodage = reponse.headers.get_content_charset() or "utf-8"
        return reponse.status, brut.decode(encodage, errors="replace")


def robots_autorise(url: str) -> bool | None:
    """Critère 4 — relu à chaque passage.

    Renvoie `None` quand le fichier est illisible : une incertitude ne doit pas
    se lire comme une autorisation.
    """
    parties = urllib.parse.urlparse(url)
    racine = f"{parties.scheme}://{parties.netloc}/robots.txt"
    try:
        _, texte = _recuperer(racine, timeout=15)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    agent_courant = False
    for ligne in texte.splitlines():
        ligne = ligne.split("#", 1)[0].strip()
        if not ligne or ":" not in ligne:
            continue
        cle, _, valeur = ligne.partition(":")
        cle, valeur = cle.strip().lower(), valeur.strip()
        if cle == "user-agent":
            # On ne retient que `*` : les agents nommés visent des robots
            # précis, et s'en réclamer serait se faire passer pour un autre.
            agent_courant = valeur == "*"
        elif cle == "disallow" and agent_courant and valeur == "/":
            return False
    return True


def relever(cle: str, config: dict[str, str]) -> Releve:
    horodatage = datetime.now(UTC).isoformat()
    autorise = robots_autorise(config["page"])

    if autorise is False:
        return Releve(
            source=cle, horodatage=horodatage, http_status=None,
            robots_autorise=False, produits=0, prix_trouves=0,
            exemples_prix=[], taille_octets=0,
            erreur="robots.txt interdit la collecte — source ignorée",
        )

    try:
        statut, html = _recuperer(config["page"])
    except Exception as exc:  # noqa: BLE001
        return Releve(
            source=cle, horodatage=horodatage, http_status=None,
            robots_autorise=autorise, produits=0, prix_trouves=0,
            exemples_prix=[], taille_octets=0, erreur=f"{type(exc).__name__}: {exc}",
        )

    prix = [
        unescape(re.sub(r"<[^>]+>", "", p))
        .strip()
        .replace(" ", " ")
        .replace("\xa0", " ")
        for p in re.findall(config["motif_prix"], html, re.S)
    ]
    prix = [p for p in prix if p]

    return Releve(
        source=cle,
        horodatage=horodatage,
        http_status=statut,
        robots_autorise=autorise,
        produits=len(re.findall(config["motif_produit"], html)),
        prix_trouves=len(prix),
        exemples_prix=prix[:3],
        taille_octets=len(html),
    )


def archiver(releves: list[Releve]) -> None:
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    with ARCHIVE.open("a", encoding="utf-8") as flux:
        for releve in releves:
            flux.write(json.dumps(asdict(releve), ensure_ascii=False) + "\n")


def charger() -> list[dict]:
    if not ARCHIVE.exists():
        return []
    return [
        json.loads(ligne)
        for ligne in ARCHIVE.read_text(encoding="utf-8").splitlines()
        if ligne.strip()
    ]


def rapport() -> int:
    passes = charger()
    if not passes:
        print("Aucun relevé archivé. Lancer d'abord :")
        print("  python scripts/spike01_releve.py")
        return 1

    par_source: dict[str, list[dict]] = {}
    for ligne in passes:
        par_source.setdefault(ligne["source"], []).append(ligne)

    print("SPIKE-01 — critère 3 : stabilité sur trois relevés hebdomadaires")
    print("=" * 68)

    valides = 0
    for cle, lignes in sorted(par_source.items()):
        nom = SOURCES.get(cle, {}).get("nom", cle)
        print()
        print(f"{nom}  ({len(lignes)} relevé(s))")
        for ligne in lignes:
            jour = ligne["horodatage"][:10]
            if ligne["erreur"]:
                print(f"  {jour}  ✗ {ligne['erreur'][:60]}")
            else:
                print(
                    f"  {jour}  {ligne['produits']:>4} produits · "
                    f"{ligne['prix_trouves']:>4} prix · ex. {ligne['exemples_prix'][:1]}"
                )

        exploitables = [l for l in lignes if not l["erreur"] and l["prix_trouves"] > 0]
        if len(exploitables) < 3:
            print(f"  → {len(exploitables)}/3 relevés exploitables — critère 3 non instruit")
            continue

        comptes = [l["prix_trouves"] for l in exploitables[-3:]]
        etendue = max(comptes) - min(comptes)
        # Un catalogue vit : quelques produits entrent et sortent. Ce qu'on
        # cherche, c'est un effondrement — le signe que le sélecteur ne
        # correspond plus à la page.
        stable = min(comptes) > 0 and etendue <= max(1, round(0.5 * max(comptes)))
        verdict = "✓ structure stable" if stable else "✗ structure instable"
        print(f"  → 3 derniers relevés : {comptes} — {verdict}")
        if stable:
            valides += 1

    print()
    print("=" * 68)
    print(f"Sources satisfaisant le critère 3 : {valides} (il en faut 2)")
    if valides >= 2:
        print("→ Critère 3 instruit. Reste le critère 2 (≥30 ingrédients du")
        print("  référentiel), qui dépend du sprint 03, et l'arbitrage CGU.")
    else:
        print("→ Le sprint 11 reste fermé. Relancer ce relevé chaque semaine.")
    return 0


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python scripts/spike01_releve.py",
        description="Relevé hebdomadaire de stabilité des sources (SPIKE-01, critère 3).",
    )
    parser.add_argument("--rapport", action="store_true", help="comparer les relevés archivés")
    parser.add_argument("--source", choices=sorted(SOURCES), help="ne relever qu'une source")
    args = parser.parse_args()

    if args.rapport:
        return rapport()

    cibles = {args.source: SOURCES[args.source]} if args.source else SOURCES
    print(f"SPIKE-01 — relevé du {datetime.now(UTC):%Y-%m-%d}")
    print(f"Agent : {AGENT}")
    print()

    releves: list[Releve] = []
    for index, (cle, config) in enumerate(cibles.items()):
        if index:
            time.sleep(DELAI_ENTRE_SOURCES_S)
        releve = relever(cle, config)
        releves.append(releve)
        if releve.erreur:
            print(f"  ✗ {config['nom']:<28} {releve.erreur[:60]}")
        else:
            print(
                f"  ✓ {config['nom']:<28} {releve.produits:>4} produits · "
                f"{releve.prix_trouves:>4} prix · ex. {releve.exemples_prix[:1]}"
            )

    archiver(releves)
    print()
    print(f"Archivé dans {ARCHIVE.relative_to(RACINE)}")
    print("Relancer dans une semaine, puis : python scripts/spike01_releve.py --rapport")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
