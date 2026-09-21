"""Contrat des routes de génération (FN-023, sprint 09).

Ces tests sont **structurels**, comme `test_isolation.py`, et pour la même
raison : ils portent sur ce qu'une route *peut* accepter ou renvoyer, pas sur un
scénario donné. Une route ajoutée demain avec un `user_id` « par commodité », ou
un `POST /generate` repassé en synchrone, les fera échouer.

Aucune base n'est nécessaire : on introspecte l'application.
"""

from fastapi import status

from app.main import app
from app.schemas.planning import GenerateIn, JobOut, PlanOut
from app.services.progress import IDENTIFYING_PARAMS


def routes_de_generation():
    trouvees = []

    def parcourir(conteneur, profondeur=0):
        if profondeur > 4:
            return
        for route in getattr(conteneur, "routes", []) or []:
            interne = getattr(route, "original_router", None)
            if interne is not None:
                parcourir(interne, profondeur + 1)
                continue
            if "meal-plans" in getattr(route, "path", ""):
                trouvees.append(route)

    parcourir(app)
    return trouvees


ROUTES = routes_de_generation()


def test_les_trois_routes_existent():
    """Garde-fou du test : une introspection vide passerait en silence."""
    chemins = {r.path for r in ROUTES}
    assert chemins == {
        "/api/v1/nutrition/meal-plans",
        "/api/v1/nutrition/meal-plans/generate",
        "/api/v1/nutrition/meal-plans/{job_id}",
    }


def test_la_generation_est_asynchrone():
    """202 Accepted, pas 200 : la chaîne dépasse régulièrement quelques
    secondes, et repasser en synchrone ferait expirer le client."""
    generate = next(r for r in ROUTES if r.path.endswith("/generate"))

    assert generate.status_code == status.HTTP_202_ACCEPTED


def test_aucune_route_n_accepte_d_identifiant_utilisateur():
    """L'utilisateur vient du claim `sub`. `job_id` est un identifiant de
    travail, pas de personne — et le job est confronté au porteur du jeton."""
    for route in ROUTES:
        noms = {p.name for p in route.dependant.path_params}
        noms |= {p.name for p in route.dependant.query_params}
        assert not (noms & IDENTIFYING_PARAMS), f"{route.path} accepte {noms}"


def test_la_demande_ne_porte_pas_d_identifiant():
    assert not (set(GenerateIn.model_fields) & IDENTIFYING_PARAMS)


def test_la_duree_est_bornee():
    """Une demande de 365 jours doit être refusée par le contrat, pas absorbée
    par le moteur."""
    champ = GenerateIn.model_fields["days"]
    contraintes = {type(m).__name__: m for m in champ.metadata}

    assert "Ge" in contraintes and "Le" in contraintes


def test_le_suivi_expose_le_motif_d_echec():
    """Un job en échec sans code ni motif obligerait le client à deviner."""
    assert {"error_code", "error_detail", "status"} <= set(JobOut.model_fields)


def test_le_plan_expose_les_creneaux_non_couverts():
    """Un catalogue sans petit-déjeuner produit un programme sans petit-déjeuner.
    Le client doit le voir, pas croire à un oubli d'affichage."""
    assert "uncovered_slots" in PlanOut.model_fields
