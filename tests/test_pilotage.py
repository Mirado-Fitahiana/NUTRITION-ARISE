"""Plateforme de pilotage — pages, fichiers statiques et gardes (plan §4).

Comme `test_console.py`, ces tests ne vérifient pas l'esthétique. Ils vérifient
ce qui rendrait la plateforme dangereuse :

* **elle n'existe pas en production** — pages et fichiers statiques compris ;
* **aucun fichier hors du dossier statique** ne peut être servi ;
* **toute action qui écrit ou déclenche exige l'administrateur** — un
  nutritionniste consulte, il ne lance pas de collecte ;
* **aucune ressource externe** n'est chargée.
"""

import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.core.auth import require_admin, require_catalog_editor
from app.core.config import settings
from app.main import app

RACINE = Path(__file__).resolve().parents[1]
PAGES = [
    "/pilotage",
    "/pilotage/collecte",
    "/pilotage/laboratoire",
    "/pilotage/reglages",
    f"/pilotage/collecte/executions/{uuid.uuid4()}",
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def cle_privee():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jeton(cle_privee, tmp_path, monkeypatch):
    publique = tmp_path / "publique.pem"
    publique.write_bytes(
        cle_privee.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "jwt_dev_public_key_path", str(publique))

    def fabriquer(role: str) -> dict[str, str]:
        maintenant = int(time.time())
        valeur = jwt.encode(
            {
                "sub": f"pilotage-{role}",
                "role": role,
                "entitlements": ["nutrition"],
                "iss": settings.jwt_issuer,
                "aud": [settings.jwt_audience],
                "iat": maintenant,
                "exp": maintenant + 600,
            },
            cle_privee,
            algorithm="RS256",
        )
        return {"Authorization": f"Bearer {valeur}"}

    return fabriquer


def routes_sous(prefixe: str):
    trouvees = []

    def parcourir(conteneur, profondeur=0):
        if profondeur > 4:
            return
        for route in getattr(conteneur, "routes", []) or []:
            interne = getattr(route, "original_router", None)
            if interne is not None:
                parcourir(interne, profondeur + 1)
                continue
            if getattr(route, "path", "").startswith(prefixe):
                trouvees.append(route)

    parcourir(app)
    return trouvees


# --------------------------------------------------------------------------
# Pages et fichiers statiques
# --------------------------------------------------------------------------


class TestPages:
    @pytest.mark.parametrize("chemin", PAGES)
    def test_pages_servies_hors_production(self, client, monkeypatch, chemin):
        monkeypatch.setattr(settings, "environment", "development")
        reponse = client.get(chemin)
        assert reponse.status_code == 200
        assert "text/html" in reponse.headers["content-type"]
        assert "Pilotage ARISE Nutrition" in reponse.text

    @pytest.mark.parametrize("chemin", PAGES + ["/pilotage/static/pilotage.css", "/pilotage/static/api.js"])
    def test_la_plateforme_disparait_en_production(self, client, monkeypatch, chemin):
        monkeypatch.setattr(settings, "environment", "production")
        # 404 et non 403 : rien ne doit laisser deviner qu'un outil interne est déployé.
        assert client.get(chemin).status_code == 404

    def test_identifiant_d_execution_invalide(self, client, monkeypatch):
        monkeypatch.setattr(settings, "environment", "development")
        assert client.get("/pilotage/collecte/executions/pas-un-uuid").status_code == 404

    @pytest.mark.parametrize(
        "chemin",
        ["/pilotage/static/..%2F..%2Fcore%2Fconfig.py", "/pilotage/static/%2e%2e/%2e%2e/main.py", "/pilotage/static/inexistant.js"],
    )
    def test_aucun_fichier_hors_du_dossier_statique(self, client, monkeypatch, chemin):
        monkeypatch.setattr(settings, "environment", "development")
        assert client.get(chemin).status_code == 404

    def test_identite_visuelle_arise(self, client, monkeypatch):
        """Jetons repris de l'application mobile (theme.ts) et du logo."""
        monkeypatch.setattr(settings, "environment", "development")
        feuille = client.get("/pilotage/static/pilotage.css")
        assert feuille.headers["content-type"].startswith("text/css")
        for jeton_visuel in ("#E72377", "#3E0E54", "#F97861", "Funnel Sans", "DM Serif Display"):
            assert jeton_visuel in feuille.text
        police = client.get("/pilotage/static/fonts/FunnelSans.ttf")
        assert (police.status_code, police.headers["content-type"]) == (200, "font/ttf")


def test_aucune_ressource_externe():
    fichiers = list((RACINE / "app" / "templates" / "pilotage").glob("*.html"))
    fichiers += list((RACINE / "app" / "static" / "pilotage").glob("*.css"))
    fichiers += list((RACINE / "app" / "static" / "pilotage").glob("*.js"))
    assert fichiers
    for fichier in fichiers:
        texte = fichier.read_text(encoding="utf-8")
        for motif in ('src="http', 'href="http', "url(http", "@import", 'fetch("http'):
            assert motif not in texte, f"{fichier.name} charge une ressource externe ({motif})"


# --------------------------------------------------------------------------
# Gardes des API de pilotage
# --------------------------------------------------------------------------

PREFIXES = (
    "/api/v1/admin/scraping",
    "/api/v1/admin/supervision",
    "/api/v1/admin/settings",
    "/api/v1/admin/scoring-weights",
    "/api/v1/admin/environment",
    "/api/v1/admin/lab",
)
ECRITURES = ("/api/v1/admin/scraping", "/api/v1/admin/settings", "/api/v1/admin/scoring-weights")


def routes_de_pilotage():
    return [r for prefixe in PREFIXES for r in routes_sous(prefixe)]


def test_les_routes_du_plan_existent():
    chemins = {r.path for r in routes_de_pilotage()}
    # §9.7 de la spécification, et plan §5.6 à §8.5.
    attendus = {
        "/api/v1/admin/scraping/run",
        "/api/v1/admin/scraping/runs",
        "/api/v1/admin/scraping/runs/{run_id}",
        "/api/v1/admin/scraping/errors",
        "/api/v1/admin/scraping/qualification",
        "/api/v1/admin/scraping/review/aliases",
        "/api/v1/admin/supervision/indicateurs",
        "/api/v1/admin/settings/{cle}",
        "/api/v1/admin/scoring-weights/{version}/activate",
        "/api/v1/admin/lab/simulate",
        "/api/v1/admin/lab/evaluations",
    }
    assert attendus <= chemins, sorted(attendus - chemins)


def test_toutes_exigent_un_editeur_du_catalogue():
    for route in routes_de_pilotage():
        assert require_catalog_editor in {d.call for d in route.dependant.dependencies}, route.path


def test_les_ecritures_exigent_l_administrateur():
    verifiees = 0
    for route in routes_de_pilotage():
        if route.path.startswith(ECRITURES) and route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
            verifiees += 1
    assert verifiees >= 10


def test_sans_jeton_401(client):
    reponse = client.get("/api/v1/admin/supervision/indicateurs")
    assert reponse.status_code == 401
    assert reponse.json()["code"] == "UNAUTHENTICATED"


def test_un_nutritionniste_ne_lance_pas_de_collecte(client, jeton):
    reponse = client.post("/api/v1/admin/scraping/run", headers=jeton("nutritionist"), json={"source": "kibo", "mode": "structure"})
    assert reponse.status_code == 403


def test_simulation_par_l_api_sur_le_catalogue_fictif(client, jeton):
    reponse = client.post(
        "/api/v1/admin/lab/simulate",
        headers=jeton("admin"),
        json={"profil": {"goal": "balanced_diet", "kcal_target": 2000, "allergens": ["peanut"]}, "jours": 2, "seed": 42, "catalogue": "fictif"},
    )
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["catalogue"]["fictif"] is True
    assert corps["composition"]["statut"] == "ok"


def test_campagne_par_l_api(client, jeton):
    reponse = client.post("/api/v1/admin/lab/evaluations", headers=jeton("nutritionist"))
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["reussis"] == corps["total"] > 0
