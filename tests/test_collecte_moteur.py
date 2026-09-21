"""Moteur de collecte (FN-013, plan §5.1) — sans réseau.

Ces tests protègent les verrous, pas le chemin passant :

* un `robots.txt` interdisant ou incertain → rien n'est relevé ;
* le délai plancher et le plafond de pages ne se contournent pas par la
  configuration ;
* aucune redirection ne fait sortir du site — la requête vers l'autre site n'est
  même pas émise ;
* l'agent annoncé est toujours celui d'ARISE, en ASCII ;
* **aucun prix n'est écrit** : les modules de collecte ne connaissent pas
  `ingredient_prices`, et c'est vérifié sur leur code.
"""

import ast
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.models.collecte import DELAI_PLANCHER_S, ScrapingSource
from app.services.collecte import moteur
from app.services.collecte.moteur import ParametresCollecte, RapporteurMemoire, SourceCollecte
from app.services.pricing import normaliser_libelle

RACINE = Path(__file__).resolve().parents[1]
HTML = (Path(__file__).parent / "fixtures" / "html" / "prestashop_categorie.html").read_text(encoding="utf-8")

PAGE_1 = "https://www.kibo.mg/tananarive/182-sucres"
PAGE_2 = "https://www.kibo.mg/tananarive/183-farines"
ROBOTS = "https://www.kibo.mg/robots.txt"
INDEX = {normaliser_libelle("Sucre"): "sucre"}


def source(**changements) -> SourceCollecte:
    valeurs = dict(
        slug="kibo",
        nom="kibo.mg",
        plateforme="prestashop",
        base_url="https://www.kibo.mg",
        pages=(PAGE_1, PAGE_2),
        delai_s=Decimal("3"),
        max_pages=5,
    )
    valeurs.update(changements)
    return SourceCollecte(**valeurs)


def page(html: str = HTML) -> httpx.Response:
    return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})


def robots(texte: str = "User-agent: *\nAllow: /\n", statut: int = 200):
    return lambda: httpx.Response(statut, text=texte)


def routes_ouvertes(**supplement):
    routes = {ROBOTS: robots(), PAGE_1: page, PAGE_2: page}
    routes.update(supplement)
    return routes


class Attente:
    """Remplace `asyncio.sleep` : on vérifie le délai demandé sans l'attendre."""

    def __init__(self) -> None:
        self.durees: list[float] = []

    async def __call__(self, secondes: float) -> None:
        self.durees.append(secondes)


async def lancer(routes, *, params=None, src=None, rapporteur=None, contact="equipe-arise@exemple.org"):
    appels: list[httpx.Request] = []

    def repondre(requete: httpx.Request) -> httpx.Response:
        appels.append(requete)
        fabrique = routes.get(str(requete.url))
        return fabrique() if fabrique else httpx.Response(404)

    rapporteur = rapporteur or RapporteurMemoire()
    attente = Attente()
    async with moteur.creer_client(contact, transport=httpx.MockTransport(repondre)) as client:
        bilan = await moteur.executer(
            src or source(),
            params or ParametresCollecte(mode="collecte"),
            client=client,
            rapporteur=rapporteur,
            index=INDEX,
            attendre=attente,
        )
    return bilan, rapporteur, appels, attente


def pages_demandees(appels) -> list[str]:
    return [str(a.url) for a in appels if not str(a.url).endswith("/robots.txt")]


# --------------------------------------------------------------------------
# Chemin passant
# --------------------------------------------------------------------------


async def test_collecte_a_blanc_nominale():
    bilan, rapporteur, _, _ = await lancer(routes_ouvertes())
    compteurs = rapporteur.derniers_compteurs

    assert bilan.statut == "succeeded"
    assert compteurs["pages_lues"] == 2
    assert compteurs["produits"] == 10
    assert compteurs["prix_trouves"] == 8
    assert compteurs["robots_autorise"] is True
    assert len(rapporteur.offres_recues) == 10
    assert [index for index, _ in rapporteur.instantanes] == [0, 1]


async def test_appariement_exact_uniquement():
    _, rapporteur, _, _ = await lancer(routes_ouvertes())
    apparies = {o.libelle for o in rapporteur.offres_recues if o.ingredient_slug}
    # « Sucre blanc cristallisé 1kg » n'est pas « Sucre » : il part en revue.
    assert apparies == {"Sucre"}
    assert rapporteur.derniers_compteurs["ingredients_apparies"] == 1


async def test_releve_de_structure_une_page_aucune_offre():
    bilan, rapporteur, appels, attente = await lancer(routes_ouvertes(), params=ParametresCollecte(mode="structure"))
    assert bilan.statut == "succeeded"
    assert pages_demandees(appels) == [PAGE_1]
    assert rapporteur.offres_recues == []
    assert attente.durees == []
    assert rapporteur.derniers_compteurs["produits"] == 5
    assert rapporteur.derniers_compteurs["prix_trouves"] == 4


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


async def test_robots_interdisant_arrete_la_source_sans_requete_de_page():
    routes = routes_ouvertes(**{ROBOTS: robots("User-agent: *\nDisallow: /tananarive/\n")})
    bilan, rapporteur, appels, _ = await lancer(routes)
    assert bilan.statut == "failed"
    assert bilan.code_erreur == "ROBOTS_REFUS"
    assert pages_demandees(appels) == []
    assert rapporteur.offres_recues == []


async def test_robots_en_erreur_vaut_refus():
    bilan, _, appels, _ = await lancer(routes_ouvertes(**{ROBOTS: robots("", statut=503)}))
    assert bilan.code_erreur == "ROBOTS_INCERTAIN"
    assert pages_demandees(appels) == []


async def test_robots_absent_ne_restreint_pas():
    bilan, _, _, _ = await lancer(routes_ouvertes(**{ROBOTS: robots("", statut=404)}))
    assert bilan.statut == "succeeded"


async def test_regle_nommee_pour_notre_agent_s_applique():
    texte = "User-agent: ARISE-NutritionBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    bilan, _, appels, _ = await lancer(routes_ouvertes(**{ROBOTS: robots(texte)}))
    assert bilan.code_erreur == "ROBOTS_REFUS"
    assert pages_demandees(appels) == []


# --------------------------------------------------------------------------
# Délai et plafond
# --------------------------------------------------------------------------


async def test_delai_plancher_non_contournable():
    _, _, _, attente = await lancer(
        routes_ouvertes(),
        src=source(delai_s=Decimal("0.5")),
        params=ParametresCollecte(mode="collecte", delai_reglage_s=Decimal("1")),
    )
    assert attente.durees == [float(DELAI_PLANCHER_S)]


async def test_le_plus_prudent_des_delais_s_applique():
    _, _, _, attente = await lancer(routes_ouvertes(), params=ParametresCollecte(mode="collecte", delai_reglage_s=Decimal("7")))
    assert attente.durees == [7.0]


async def test_plafond_de_pages():
    rayons = tuple(f"https://www.kibo.mg/tananarive/{i}-rayon" for i in range(6))
    routes = {ROBOTS: robots(), **{adresse: page for adresse in rayons}}
    _, _, appels, _ = await lancer(routes, src=source(pages=rayons), params=ParametresCollecte(mode="collecte", max_pages_reglage=2))
    assert len(pages_demandees(appels)) == 2


def test_le_plancher_est_aussi_porte_par_la_base():
    contraintes = [str(c.sqltext) for c in ScrapingSource.__table__.constraints if hasattr(c, "sqltext")]
    assert any("delay_seconds >= 3" in c for c in contraintes)
    assert DELAI_PLANCHER_S == 3


# --------------------------------------------------------------------------
# Périmètre du site
# --------------------------------------------------------------------------


async def test_redirection_hors_site_jamais_suivie():
    routes = routes_ouvertes(
        **{
            PAGE_1: lambda: httpx.Response(302, headers={"location": "https://ailleurs.example/piege"}),
            "https://ailleurs.example/piege": page,
        }
    )
    _, rapporteur, appels, _ = await lancer(routes)
    assert "ailleurs.example" not in {a.url.host for a in appels}
    assert any("redirection hors du site" in e["message"] for e in rapporteur.erreurs)
    assert rapporteur.derniers_compteurs["pages_lues"] == 1


async def test_redirection_interne_suivie():
    destination = "https://www.kibo.mg/tananarive/182-sucres-et-edulcorants"
    routes = routes_ouvertes(
        **{
            PAGE_1: lambda: httpx.Response(301, headers={"location": "/tananarive/182-sucres-et-edulcorants"}),
            destination: page,
        }
    )
    _, rapporteur, _, _ = await lancer(routes)
    assert rapporteur.derniers_compteurs["pages_lues"] == 2


async def test_redirection_vers_une_page_interdite_refusee():
    routes = routes_ouvertes(
        **{
            ROBOTS: robots("User-agent: *\nDisallow: /prive/\n"),
            PAGE_1: lambda: httpx.Response(302, headers={"location": "/prive/tarifs"}),
            "https://www.kibo.mg/prive/tarifs": page,
        }
    )
    _, rapporteur, appels, _ = await lancer(routes)
    assert "https://www.kibo.mg/prive/tarifs" not in pages_demandees(appels)
    assert any("non autorisée" in e["message"] for e in rapporteur.erreurs)


async def test_page_configuree_hors_site_ignoree_sans_requete():
    src = source(pages=("https://ailleurs.example/catalogue", PAGE_1))
    _, rapporteur, appels, _ = await lancer(routes_ouvertes(), src=src)
    assert "ailleurs.example" not in {a.url.host for a in appels}
    assert rapporteur.derniers_compteurs["pages_lues"] == 1


# --------------------------------------------------------------------------
# Annulation, alertes, agent
# --------------------------------------------------------------------------


async def test_annulation_avant_la_page_suivante():
    bilan, _, appels, _ = await lancer(routes_ouvertes(), rapporteur=RapporteurMemoire(annuler_apres=1))
    assert bilan.statut == "cancelled"
    assert pages_demandees(appels) == [PAGE_1]


def miniature(libelle: str, prix: str) -> str:
    return (
        '<article class="product-miniature js-product-miniature">'
        f'<h2 class="product-title"><a href="/p">{libelle}</a></h2>'
        f'<span class="price">{prix}</span></article>'
    )


async def test_alerte_de_structure_au_dela_du_seuil():
    illisible = "".join(miniature(f"Produit {i}", "Prix sur demande") for i in range(4)) + miniature("Produit 5", "2 000 Ar")
    bilan, rapporteur, _, _ = await lancer(
        routes_ouvertes(**{PAGE_1: lambda: page(illisible)}), params=ParametresCollecte(mode="structure")
    )
    assert bilan.statut == "succeeded"
    assert bilan.alerte is True
    assert rapporteur.derniers_compteurs["taux_echec_extraction"] == pytest.approx(0.8)


async def test_page_sans_produit_leve_une_alerte():
    vide = "<html><body><p>Site en maintenance</p></body></html>"
    bilan, rapporteur, _, _ = await lancer(routes_ouvertes(**{PAGE_1: lambda: page(vide)}), params=ParametresCollecte(mode="structure"))
    assert bilan.alerte is True
    assert any("Aucun bloc produit" in message for _, _, message in rapporteur.evenements)


async def test_agent_arise_annonce_sur_chaque_requete():
    _, _, appels, _ = await lancer(routes_ouvertes(), contact="équipe ARISE <contact@exemple.org>")
    agents = {a.headers["user-agent"] for a in appels}
    assert len(agents) == 1
    agent = agents.pop()
    assert agent.startswith("ARISE-NutritionBot/")
    assert agent.isascii()
    assert "contact@exemple.org" in agent


async def test_rejeu_sans_aucune_requete():
    rapporteur = RapporteurMemoire()
    bilan = await moteur.rejouer(source(), [(PAGE_1, HTML)], ParametresCollecte(mode="collecte"), rapporteur=rapporteur, index=INDEX)
    assert bilan.statut == "succeeded"
    assert len(rapporteur.offres_recues) == 5


# --------------------------------------------------------------------------
# Verrou n° 1 — aucun prix écrit
# --------------------------------------------------------------------------


def _fichiers_de_collecte() -> list[Path]:
    fichiers = sorted((RACINE / "app" / "services" / "collecte").rglob("*.py"))
    fichiers += [
        RACINE / "app" / "services" / "executions.py",
        RACINE / "app" / "routers" / "admin_collecte.py",
        RACINE / "app" / "models" / "collecte.py",
    ]
    return fichiers


def _docstrings(arbre: ast.AST) -> set[int]:
    ids = set()
    for noeud in ast.walk(arbre):
        corps = getattr(noeud, "body", None)
        if (
            isinstance(noeud, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and corps
            and isinstance(corps[0], ast.Expr)
            and isinstance(corps[0].value, ast.Constant)
            and isinstance(corps[0].value.value, str)
        ):
            ids.add(id(corps[0].value))
    return ids


@pytest.mark.parametrize("fichier", _fichiers_de_collecte(), ids=lambda p: str(p.relative_to(RACINE)))
def test_aucun_module_de_collecte_n_ecrit_de_prix(fichier):
    """Structurel : un import de `IngredientPrice`, ou une requête vers
    `ingredient_prices`, ajouté demain dans la collecte fera échouer ce test."""
    arbre = ast.parse(fichier.read_text(encoding="utf-8"))
    documentation = _docstrings(arbre)
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.alias):
            assert "IngredientPrice" not in (noeud.name, noeud.asname), fichier
        elif isinstance(noeud, ast.Name):
            assert noeud.id != "IngredientPrice", fichier
        elif isinstance(noeud, ast.Attribute):
            assert noeud.attr != "IngredientPrice", fichier
        elif isinstance(noeud, ast.Constant) and isinstance(noeud.value, str) and id(noeud) not in documentation:
            assert "ingredient_prices" not in noeud.value, fichier
