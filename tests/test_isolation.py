"""Isolation des données utilisateur (FN-001, §14.5).

Le critère de fin du lot 1 dit : « un utilisateur ne peut accéder qu'à ses
propres données (test automatisé) ». Ce fichier est ce test.

Il est **structurel** plutôt que fonctionnel, et c'est délibéré : un test
fonctionnel vérifierait qu'Alice ne lit pas le profil de Victor sur les routes
d'aujourd'hui. Celui-ci vérifie qu'aucune route ne *peut* désigner un autre
utilisateur — donc il échouera sur une route ajoutée demain sans garde, ce
qu'un test fonctionnel ne ferait pas.

Aucune base n'est nécessaire : on introspecte l'application.
"""

import pytest

from app.core.auth import current_user
from app.main import app
from app.services.progress import IDENTIFYING_PARAMS, _profile_routes_are_self_scoped


def routes_under(prefix: str):
    """Aplati les routeurs inclus, que FastAPI conserve imbriqués."""
    found = []

    def walk(container, depth=0):
        if depth > 4:
            return
        for route in getattr(container, "routes", []) or []:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner, depth + 1)
                continue
            if getattr(route, "path", "").startswith(prefix):
                found.append(route)

    walk(app)
    return found


PROFILE_ROUTES = routes_under("/api/v1/nutrition")


def test_des_routes_de_profil_existent():
    """Garde-fou du test lui-même : une introspection qui ne trouve rien
    passerait silencieusement."""
    assert PROFILE_ROUTES, "aucune route de profil enregistrée"


@pytest.mark.parametrize("route", PROFILE_ROUTES, ids=lambda r: r.path)
class TestChaqueRouteDeProfil:
    def test_n_accepte_aucun_identifiant_utilisateur(self, route):
        """Le profil est résolu depuis le claim `sub`. Accepter un identifiant
        en chemin ou en requête rouvrirait la porte."""
        names = {p.name for p in route.dependant.path_params}
        names |= {p.name for p in route.dependant.query_params}
        interdits = names & IDENTIFYING_PARAMS
        assert not interdits, f"{route.path} accepte {sorted(interdits)}"

    def test_exige_un_utilisateur_authentifie(self, route):
        calls = {sub.call for sub in route.dependant.dependencies}
        assert current_user in calls, f"{route.path} n'exige pas current_user"


class TestAdministrationProtegee:
    """Les routes d'administration ne doivent jamais être ouvertes : le
    catalogue est la source de vérité de tout l'écosystème (D-01)."""

    def test_toutes_les_routes_admin_ont_une_garde_de_role(self):
        from app.core.auth import require_catalog_editor

        sans_garde = []
        for route in routes_under("/api/v1/admin"):
            calls = {sub.call for sub in route.dependant.dependencies}
            if require_catalog_editor not in calls:
                sans_garde.append(route.path)
        assert not sans_garde, f"routes admin sans garde : {sans_garde}"

    def test_la_signature_des_allergenes_est_reservee_au_nutritionniste(self):
        """FN-003 — c'est l'acte qui engage une responsabilité ; un
        administrateur ne peut pas le poser."""
        from app.core.auth import require_validator

        cible = [
            r
            for r in routes_under("/api/v1/admin")
            if r.path.endswith("/verify-allergens")
        ]
        assert cible, "point d'entrée de signature absent"
        for route in cible:
            calls = {sub.call for sub in route.dependant.dependencies}
            assert require_validator in calls

    def test_la_publication_est_reservee_au_nutritionniste(self):
        from app.core.auth import require_validator

        cible = [
            r for r in routes_under("/api/v1/admin") if r.path.endswith("/publish")
        ]
        assert cible, "point d'entrée de publication absent"
        for route in cible:
            calls = {sub.call for sub in route.dependant.dependencies}
            assert require_validator in calls


class TestSondeDAvancement:
    """La console affiche ce critère comme « fait ». Elle doit dire vrai."""

    def test_la_sonde_confirme_l_isolation(self):
        ok, detail = _profile_routes_are_self_scoped(app)
        assert ok, detail
