"""Cas d'évaluation du laboratoire (plan §8.4).

Chaque cas de `evaluation/cas` est rejoué ici, à chaque commit, avec les mêmes
conditions que l'écran « Lancer la campagne » : ce qui passe au vert à l'écran
passe au vert ici, et réciproquement.
"""

import pytest

from app.services.evaluation import CasInvalide, charger_cas, executer_cas
from app.services.laboratoire import charger_catalogue_fictif

PLATS, _ = charger_catalogue_fictif()
CAS = charger_cas()


def test_des_cas_existent():
    """Garde-fou : un dossier vide passerait en silence."""
    assert len(CAS) >= 10


@pytest.mark.parametrize("cas", CAS, ids=lambda c: c.id)
def test_cas_d_evaluation(cas):
    resultat = executer_cas(cas, PLATS)
    echecs = [f"{a['nom']} : {a['detail']}" for a in resultat["assertions"] if not a["ok"]]
    assert resultat["ok"], " · ".join(echecs)


def test_une_attente_inconnue_fait_echouer_le_cas(tmp_path):
    (tmp_path / "faute.yaml").write_text(
        "- id: faute-de-frappe\n"
        "  profil: {goal: balanced_diet, kcal_target: 2000}\n"
        "  attendu:\n"
        "    succes: true\n"
        "    aucun_alergene: [peanut]\n",
        encoding="utf-8",
    )
    resultat = executer_cas(charger_cas(tmp_path)[0], PLATS)
    assert resultat["ok"] is False
    assert any("attente inconnue" in a["detail"] for a in resultat["assertions"])


def test_un_cas_sans_attendu_est_refuse(tmp_path):
    (tmp_path / "vide.yaml").write_text("- id: rien\n  profil: {goal: balanced_diet}\n", encoding="utf-8")
    with pytest.raises(CasInvalide):
        charger_cas(tmp_path)
