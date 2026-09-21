"""Tableau de bord de supervision (FN-035, plan §6).

La règle qui compte : **« aucune donnée » n'est jamais un zéro.** Un taux
d'échec sur zéro génération n'existe pas ; un tableau de bord base coupée ne
montre que des « inconnu ».
"""

from datetime import date

from app.services import supervision
from app.services.supervision import ALERTE, CRITIQUE, INCONNU, OK


def test_incidents_de_securite():
    assert supervision.etat_incidents(None) == INCONNU
    assert supervision.etat_incidents(0) == OK
    assert supervision.etat_incidents(2) == CRITIQUE


def test_ingredients_non_signes():
    assert supervision.etat_non_verifies(0, 0) == INCONNU
    assert supervision.etat_non_verifies(8, 8) == ALERTE
    assert supervision.etat_non_verifies(8, 0) == OK


def test_taux_d_echec_sans_generation_est_inconnu():
    assert supervision.etat_taux_echec(0, 0) == INCONNU
    assert supervision.etat_taux_echec(1, 10) == OK
    assert supervision.etat_taux_echec(6, 10) == ALERTE
    assert supervision.ratio(3, 0) is None
    assert supervision.ratio(None, 5) is None


def test_migration_en_retard():
    assert supervision.etat_migration("a", "a") == OK
    assert supervision.etat_migration("a", "b") == ALERTE
    assert supervision.etat_migration(None, "b") == INCONNU


def test_serie_journaliere_complete_par_des_zeros_mesures():
    serie = supervision.serie_journaliere({date(2026, 9, 14): {"total": 3}}, date(2026, 9, 13), date(2026, 9, 15), ("total",))
    assert [p["total"] for p in serie] == [0, 3, 0]


def test_motifs_d_erreur_lisibles():
    assert "alembic upgrade head" in supervision.motif_erreur(Exception('relation "operation_runs" does not exist'))
    assert "DATABASE_" in supervision.motif_erreur(Exception("authentification par mot de passe échouée"))


def test_la_tete_des_migrations_est_celle_de_la_plateforme():
    assert supervision.tete_alembic() == "4c7a1e2f9b30"


async def test_base_coupee_aucun_zero(monkeypatch):
    import app.core.database as database

    async def injoignable() -> bool:
        return False

    monkeypatch.setattr(database, "check_database", injoignable)
    resultat = await supervision.mesurer(None, periode_jours=7)

    assert resultat["base_joignable"] is False
    indicateurs = [i for bloc in resultat["blocs"] for i in bloc["indicateurs"]]
    assert indicateurs
    assert all(i["valeur"] is None and i["etat"] == INCONNU for i in indicateurs)
    assert resultat["resume"]["ok"] == 0
