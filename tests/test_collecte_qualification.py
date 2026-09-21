"""SPIKE-01 mesuré (§6.5) — aucun critère ne passe au vert par déclaration.

Le test qui compte le plus est le premier : sur l'archive réelle de
`spike01_releve.py`, la plateforme doit rendre le même verdict que le script —
un relevé sur trois, critère non instruit.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.collecte.qualification import (
    A_FAIRE,
    FAIT,
    INCONNU,
    PARTIEL,
    EtatSource,
    Releve,
    critere_2,
    critere_3,
    critere_4,
    lire_archive_spike01,
    qualifier,
    structure_stable,
)

RACINE = Path(__file__).resolve().parents[1]
DEBUT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def releve(jours: float = 0, prix: int = 17, **autres) -> Releve:
    valeurs = dict(
        horodatage=DEBUT + timedelta(days=jours),
        mode="structure",
        reussi=True,
        produits=prix,
        prix_trouves=prix,
        robots_autorise=True,
    )
    valeurs.update(autres)
    return Releve(**valeurs)


def releves_de_l_archive(slug: str) -> tuple[Releve, ...]:
    lignes = lire_archive_spike01((RACINE / "spike01" / "releves.jsonl").read_text(encoding="utf-8"))
    return tuple(
        Releve(
            horodatage=l["horodatage"],
            mode="structure",
            reussi=l["erreur"] is None,
            produits=l["produits"],
            prix_trouves=l["prix_trouves"],
            exemples_prix=tuple(l["exemples_prix"]),
            robots_autorise=l["robots_autorise"],
            erreur=l["erreur"],
        )
        for l in lignes
        if l["source"] == slug
    )


class TestArchiveReelle:
    def test_meme_verdict_que_spike01_releve_rapport(self):
        for slug in ("kibo", "abcie"):
            critere, retenus = critere_3(releves_de_l_archive(slug))
            assert critere.etat == PARTIEL
            assert "1/3" in critere.preuve and "non instruit" in critere.preuve
            assert len(retenus) == 1

    def test_aucune_source_qualifiee_aujourd_hui(self):
        resultat = qualifier(
            [EtatSource(slug=s, nom=s, actif=True, releves=releves_de_l_archive(s)) for s in ("kibo", "abcie")],
            referentiel_ingredients=8,
            contact_renseigne=False,
        )
        assert resultat.qualifiees == 0
        assert "reste fermé" in resultat.verdict

    def test_ligne_illisible_ignoree(self):
        valide = '{"source": "kibo", "horodatage": "2026-09-15T12:25:30+00:00", "prix_trouves": 3}'
        assert len(lire_archive_spike01("{pas du json\n" + valide + "\n\n")) == 1


class TestCritere3:
    def test_trois_releves_hebdomadaires_stables(self):
        critere, _ = critere_3((releve(0, 17), releve(7, 18), releve(14, 16)))
        assert critere.etat == FAIT

    def test_releves_rapproches_ne_comptent_qu_une_fois(self):
        """Relancer trois fois le même après-midi n'instruit pas le critère."""
        critere, retenus = critere_3((releve(0), releve(0.05), releve(2), releve(3)))
        assert len(retenus) == 1
        assert critere.etat == PARTIEL

    def test_effondrement_du_nombre_de_prix(self):
        critere, _ = critere_3((releve(0, 17), releve(7, 16), releve(14, 2)))
        assert critere.etat == A_FAIRE

    def test_releve_en_erreur_non_exploitable(self):
        critere, retenus = critere_3((releve(0, erreur="HTTP 500", reussi=False),))
        assert retenus == []
        assert critere.etat == A_FAIRE

    def test_regle_de_stabilite_du_script(self):
        assert structure_stable([17, 17, 17])
        assert structure_stable([1, 2, 1])
        assert not structure_stable([20, 4, 20])
        assert not structure_stable([0, 5, 5])


class TestCriteres2Et4:
    def test_critere_2_inconnu_sans_collecte(self):
        assert critere_2((releve(0),), referentiel=8).etat == INCONNU

    def test_critere_2_plafonne_par_le_referentiel(self):
        critere = critere_2((releve(0, mode="collecte", ingredients_apparies=5),), referentiel=8)
        assert critere.etat == PARTIEL
        assert "plafonné" in critere.preuve

    def test_critere_4_jamais_vert_sans_attestation_ni_contact(self):
        sans_releve = EtatSource("kibo", "kibo.mg", True, ())
        assert critere_4(sans_releve, contact_renseigne=True).etat == INCONNU

        interdit = EtatSource("kibo", "kibo.mg", True, (releve(0, robots_autorise=False),))
        assert critere_4(interdit, contact_renseigne=True).etat == A_FAIRE

        autorise = (releve(0),)
        assert critere_4(EtatSource("kibo", "kibo.mg", True, autorise), contact_renseigne=True).etat == PARTIEL

        atteste = EtatSource("kibo", "kibo.mg", True, autorise, cgu_attestees_par="admin", cgu_attestees_le=DEBUT)
        assert critere_4(atteste, contact_renseigne=False).etat == PARTIEL
        assert critere_4(atteste, contact_renseigne=True).etat == FAIT

    def test_source_sans_releve_non_qualifiee(self):
        resultat = qualifier([EtatSource("kibo", "kibo.mg", True, ())], referentiel_ingredients=150, contact_renseigne=True)
        source = resultat.sources[0]
        assert not source.qualifiee
        assert all(c.etat != FAIT for c in source.criteres)
