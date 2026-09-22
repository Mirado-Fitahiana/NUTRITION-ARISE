"""Menu du jour : remplacement d'un repas et signalement d'allergie (FN-025,
FN-031, FN-032).

Le remplacement protège la même chose que la génération, dans le même ordre :
un allergène ne revient jamais, un plat refusé n'est pas reproposé, et la même
demande donne la même réponse. Aucune base n'est nécessaire.
"""

from decimal import Decimal

from app.models.enums import Allergen, DishStatus, Goal, MealSlot
from app.services.catalog import (
    ALLOWED_TRANSITIONS,
    INCIDENT_REVIEW_TRANSITION,
    can_transition,
)
from app.services.recommandation import (
    ContraintesUtilisateur,
    filtrer,
    proposer_remplacement,
)
from tests.test_recommandation import plat

CONTRAINTES = ContraintesUtilisateur(goal=Goal.BALANCED_DIET, kcal_target=Decimal("2000"))
KCAL_DEJEUNER = Decimal("700")


def remplacer(plats, *, exclus=frozenset(), contraintes=CONTRAINTES, **kwargs):
    return proposer_remplacement(
        filtrer(plats, contraintes),
        MealSlot.LUNCH,
        KCAL_DEJEUNER,
        contraintes,
        exclus=exclus,
        **kwargs,
    )


class TestRemplacement:
    def test_ne_repropose_jamais_le_plat_remplace(self):
        choix = remplacer(
            [plat("romazava", kcal="700"), plat("ravitoto", kcal="400")],
            exclus=frozenset({"romazava"}),
        )
        assert choix is not None
        assert choix.plat.slug == "ravitoto"

    def test_un_allergene_ne_revient_pas_par_le_remplacement(self):
        """Le remplacement part du vivier filtré : même le meilleur score ne
        rouvre pas la porte à un allergène déclaré."""
        allergique = ContraintesUtilisateur(
            goal=Goal.BALANCED_DIET,
            kcal_target=Decimal("2000"),
            allergens=frozenset({Allergen.PEANUT}),
        )
        choix = remplacer(
            [
                plat("mafe", kcal="700", allergens=frozenset({Allergen.PEANUT})),
                plat("vary-sosoa", kcal="200"),
            ],
            exclus=frozenset({"ancien"}),
            contraintes=allergique,
        )
        assert choix is not None
        assert choix.plat.slug == "vary-sosoa"

    def test_aucun_autre_plat_renvoie_none(self):
        """Mieux vaut dire qu'il n'y a rien que reproposer un plat écarté."""
        assert remplacer([plat("seul")], exclus=frozenset({"seul"})) is None

    def test_respecte_le_creneau(self):
        choix = remplacer(
            [
                plat("mofo-gasy", kcal="700", types=frozenset({"breakfast"})),
                plat("henakisoa", kcal="650", types=frozenset({"lunch"})),
            ]
        )
        assert choix is not None
        assert choix.plat.slug == "henakisoa"

    def test_est_deterministe(self):
        plats = [plat(f"plat-{i}", kcal="700") for i in range(6)]
        premiers = {remplacer(plats).plat.slug for _ in range(5)}
        assert premiers == {"plat-0"}

    def test_un_plat_deja_tres_servi_passe_apres(self):
        choix = remplacer(
            [plat("souvent", kcal="700"), plat("rare", kcal="700")],
            utilisations={"souvent": 3},
        )
        assert choix.plat.slug == "rare"


class TestMiseEnRevueApresIncident:
    def test_la_transition_d_incident_retire_le_plat_des_recommandations(self):
        depart, arrivee = INCIDENT_REVIEW_TRANSITION
        assert depart is DishStatus.PUBLISHED
        assert arrivee is DishStatus.PENDING_VALIDATION

    def test_elle_n_est_pas_offerte_a_la_main(self):
        """Hors incident, un plat publié ne va toujours qu'aux archives."""
        assert not can_transition(*INCIDENT_REVIEW_TRANSITION)
        assert ALLOWED_TRANSITIONS[DishStatus.PUBLISHED] == frozenset({DishStatus.ARCHIVED})

    def test_la_republication_repasse_par_la_validation(self):
        assert can_transition(DishStatus.PENDING_VALIDATION, DishStatus.VALIDATED)
        assert not can_transition(DishStatus.PENDING_VALIDATION, DishStatus.PUBLISHED)


class TestQualiteDesRecommandations:
    """Supervision : le taux d'acceptation se lit sur les décisions, pas sur
    les repas encore en attente."""

    def test_un_repas_en_attente_ne_fait_pas_baisser_le_taux(self):
        from app.services.supervision import taux_acceptation

        assert taux_acceptation({"followed": 3, "skipped": 1, "pending": 20}) == 0.75

    def test_une_alternative_demandee_compte_comme_non_acceptee(self):
        from app.services.supervision import taux_acceptation

        assert taux_acceptation({"followed": 1, "replaced": 1}) == 0.5

    def test_sans_decision_le_taux_est_inconnu_pas_nul(self):
        from app.services.supervision import taux_acceptation

        assert taux_acceptation({"pending": 5}) is None


class TestLibellesUtilisateur:
    def test_aucun_identifiant_technique_dans_la_justification(self):
        from app.models.enums import Goal
        from app.services.recommandation import (
            LIBELLES_OBJECTIF,
            ContraintesUtilisateur,
            ProgrammeGenere,
            justification_de_repli,
        )

        programme = ProgrammeGenere(journees=(), seed=1, vivier_taille=1)
        for objectif in Goal:
            texte = justification_de_repli(
                programme,
                ContraintesUtilisateur(goal=objectif, kcal_target=Decimal("1832")),
            )
            assert str(objectif) not in texte, f"{objectif} s'affiche brut"
            assert objectif.value in LIBELLES_OBJECTIF


class TestRepasSignales:
    def test_le_contrat_expose_flagged(self):
        from app.schemas.planning import MealOut

        assert MealOut.model_fields["flagged"].default is False
