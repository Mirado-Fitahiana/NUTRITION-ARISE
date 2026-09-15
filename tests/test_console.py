"""Banc d'essai interne (FN-034).

Ces tests ne vérifient pas l'esthétique de la page : ils vérifient les deux
choses qui rendraient la console dangereuse ou trompeuse.

* **Elle ne doit pas exister en production.** C'est un outil non authentifié qui
  expose l'état interne du service et sait écrire en base.
* **Elle ne doit pas embellir ce qu'elle mesure.** Un refus de conversion, un
  refus de publication ou une exception non prévue doivent apparaître tels
  quels, avec l'enveloppe d'erreur réelle du service (FN-037).

Aucun point d'entrée testé ici ne touche PostgreSQL : la suite reste exécutable
en une seconde, sans base.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.errors import ErrorCode
from app.main import app
from app.models.enums import Allergen
from app.routers.console import dev_only


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def tolerant_client() -> TestClient:
    """`TestClient` relance par défaut les exceptions du serveur, ce qui court-
    circuite le gestionnaire global. Pour observer l'enveloppe réellement
    envoyée au client, il faut le lui interdire."""
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestRefusEnProduction:
    def test_la_console_disparait_en_production(self, monkeypatch):
        monkeypatch.setattr(settings, "environment", "production")
        with pytest.raises(HTTPException) as exc:
            dev_only()
        # 404 et non 403 : rien ne doit laisser deviner qu'un outil interne
        # est déployé à cette adresse.
        assert exc.value.status_code == 404

    def test_la_console_repond_hors_production(self, monkeypatch):
        monkeypatch.setattr(settings, "environment", "development")
        assert dev_only() is None


class TestPage:
    def test_la_page_est_servie(self, client):
        response = client.get("/console")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_les_listes_viennent_des_enumerations(self, client):
        """La page ne peut pas proposer une valeur que le domaine ignore : les
        cases d'allergènes sont rendues depuis `Allergen`."""
        body = client.get("/console").text
        for allergen in Allergen:
            assert allergen.value in body


class TestConversion:
    def test_conversion_locale_mesuree(self, client):
        response = client.post(
            "/console/api/units/convert",
            json={
                "quantity": "1",
                "unit": "kapoaka",
                "reference_unit": "g",
                "conversions": [
                    {"from_unit": "kapoaka", "to_unit": "g", "factor": "285"}
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["reference_quantity"] == "285"

    def test_conversion_absente_refusee_avec_enveloppe(self, client):
        """FN-012 : la console ne doit surtout pas absorber le refus."""
        response = client.post(
            "/console/api/units/convert",
            json={"quantity": "1", "unit": "botte", "reference_unit": "g"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == ErrorCode.VALIDATION_ERROR.value
        assert "aucune conversion mesurée" in body["message"]
        assert body["requestId"]


class TestDerivation:
    def test_ingredient_non_signe_bloque_la_publication(self, client):
        """FN-003 — le catalogue de démarrage n'est pas signé : tout plat qui en
        vient doit être déclaré non publiable."""
        response = client.post(
            "/console/api/dishes/derive",
            json={
                "servings": 4,
                "components": [
                    {"ingredient": "riz-blanc-cru", "quantity": "300", "unit": "g"},
                    {"ingredient": "huile-arachide", "quantity": "20", "unit": "ml"},
                ],
            },
        )
        assert response.status_code == 200
        derived = response.json()["derived"]
        assert derived["publishable"] is False
        assert "riz-blanc-cru" in derived["unverified_ingredients"]
        # L'allergène de l'huile d'arachide remonte au plat.
        assert derived["allergens"] == ["peanut"]

    def test_ingredient_signe_rend_le_plat_publiable(self, client):
        """Le chemin inverse, seul moyen de l'éprouver tant que le catalogue
        versionné n'est pas validé par le nutritionniste."""
        response = client.post(
            "/console/api/dishes/derive",
            json={
                "servings": 2,
                "components": [
                    {
                        "custom": {
                            "slug": "poulet-verifie",
                            "reference_unit": "g",
                            "kcal_100": "165",
                            "protein_100": "31",
                            "carbs_100": "0",
                            "fat_100": "3.6",
                            "is_allergen_verified": True,
                        },
                        "quantity": "300",
                        "unit": "g",
                    }
                ],
            },
        )
        assert response.status_code == 200
        derived = response.json()["derived"]
        assert derived["publishable"] is True
        assert derived["unverified_ingredients"] == []
        assert "high_protein" in derived["derived_tags"]

    def test_origine_de_l_ingredient_exclusive(self, client):
        response = client.post(
            "/console/api/dishes/derive",
            json={
                "servings": 1,
                "components": [{"quantity": "100", "unit": "g"}],
            },
        )
        assert response.status_code == 422

    def test_portions_nulles_refusees(self, client):
        response = client.post(
            "/console/api/dishes/derive",
            json={
                "servings": 0,
                "components": [
                    {"ingredient": "riz-blanc-cru", "quantity": "100", "unit": "g"}
                ],
            },
        )
        assert response.status_code == 422
        assert "strictement positif" in response.json()["message"]


class TestSeed:
    def test_dry_run_signale_les_publications_refusees(self, client):
        """Le refus est le comportement attendu de FN-003 tant que les
        allergènes ne sont pas signés — pas une panne du chargeur."""
        response = client.post("/console/api/seed/dry-run")
        assert response.status_code == 200
        report = response.json()
        assert report["ok"] is True
        assert report["dishes_published"] == 0
        assert report["publication_blocked"]

    def test_le_catalogue_versionne_est_lisible_sans_base(self, client):
        response = client.get("/console/api/seed/catalogue")
        assert response.status_code == 200
        body = response.json()
        assert body["errors"] == []
        assert body["ingredients"]
        # Les unités locales mesurées doivent être proposées à la saisie.
        riz = next(i for i in body["ingredients"] if i["slug"] == "riz-blanc-cru")
        assert "kapoaka" in riz["units"]


class TestEnveloppeErreur:
    @pytest.mark.parametrize(
        ("code", "http_status"),
        [
            (ErrorCode.PROFILE_INCOMPLETE, 400),
            (ErrorCode.QUOTA_EXCEEDED, 429),
            (ErrorCode.UNAUTHENTICATED, 401),
            (ErrorCode.SERVICE_UNAVAILABLE, 503),
        ],
    )
    def test_chaque_code_produit_son_statut(self, client, code, http_status):
        response = client.get(f"/console/api/errors/{code.value}")
        assert response.status_code == http_status
        body = response.json()
        assert body["code"] == code.value
        # Alignement champ pour champ sur le filtre global NestJS (FN-037).
        for field in ("statusCode", "timestamp", "path", "method", "message", "error"):
            assert field in body

    def test_une_exception_non_prevue_ne_fuit_pas(self, tolerant_client):
        """Le message d'origine est journalisé, jamais renvoyé."""
        response = tolerant_client.get(
            "/console/api/errors/unhandled", headers={"X-Request-ID": "test-fuite"}
        )
        assert response.status_code == 500
        body = response.json()
        assert body["code"] == ErrorCode.INTERNAL_ERROR.value
        assert "défaut simulé" not in body["message"]
        assert body["requestId"] == "test-fuite"
