"""Moteur de recommandation (FN-019 → FN-024, D-06).

Ces tests protègent trois choses, dans cet ordre de gravité :

1. **Un allergène ne doit jamais atteindre un programme.** Pas « rarement » :
   jamais. Le filtrage précède le score, et la validation finale le rejoue —
   les deux sont vérifiés ici, séparément, parce qu'ils protègent de causes
   différentes.
2. **Un vivier insuffisant produit un refus, pas un programme dégradé.** Avec un
   catalogue jeune, c'est le cas le plus probable — et le plus tentant à
   contourner.
3. **La génération est reproductible.** Sans cela, une régression de qualité est
   indébuggable.

Aucune base n'est nécessaire.
"""

from decimal import Decimal

import pytest

from app.models.enums import Allergen, Goal, MealSlot, RestrictionType
from app.services.recommandation import (
    ContraintesUtilisateur,
    EchecGeneration,
    PlatCandidat,
    ProgrammeGenere,
    ReglesComposition,
    composer,
    filtrer,
    justification_de_repli,
    scorer,
    valider,
)

TOUS_LES_TYPES = frozenset({"breakfast", "lunch", "dinner", "snack"})


def plat(
    slug: str,
    *,
    kcal: str = "500",
    types: frozenset[str] = TOUS_LES_TYPES,
    allergens: frozenset[Allergen] = frozenset(),
    restrictions: frozenset[RestrictionType] = frozenset(),
    goals: frozenset[Goal] = frozenset(),
    ingredients: frozenset[str] = frozenset(),
    proteines: frozenset[str] = frozenset(),
    cout: str | None = None,
) -> PlatCandidat:
    return PlatCandidat(
        slug=slug,
        name=slug.replace("-", " ").title(),
        meal_types=types,
        kcal=Decimal(kcal),
        allergens=allergens,
        compatible_restrictions=restrictions,
        compatible_goals=goals,
        ingredient_slugs=ingredients,
        protein_sources=proteines,
        estimated_cost=Decimal(cout) if cout else None,
    )


def catalogue(n: int, **kwargs) -> list[PlatCandidat]:
    return [plat(f"plat-{i:03d}", **kwargs) for i in range(n)]


CONTRAINTES = ContraintesUtilisateur(
    goal=Goal.BALANCED_DIET, kcal_target=Decimal("2000")
)
REGLES = ReglesComposition()


class TestFiltrageDur:
    def test_un_allergene_declare_exclut_le_plat(self):
        vivier = filtrer(
            [plat("arachide", allergens=frozenset({Allergen.PEANUT})), plat("sans")],
            ContraintesUtilisateur(
                goal=Goal.BALANCED_DIET,
                kcal_target=Decimal("2000"),
                allergens=frozenset({Allergen.PEANUT}),
            ),
        )

        assert [p.slug for p in vivier.retenus] == ["sans"]
        assert vivier.exclus[0].slug == "arachide"
        assert "allergène" in vivier.exclus[0].motif

    def test_une_restriction_non_satisfaite_exclut(self):
        vivier = filtrer(
            [
                plat("viande"),
                plat("legumes", restrictions=frozenset({RestrictionType.VEGETARIAN})),
            ],
            ContraintesUtilisateur(
                goal=Goal.BALANCED_DIET,
                kcal_target=Decimal("2000"),
                restrictions=frozenset({RestrictionType.VEGETARIAN}),
            ),
        )

        assert [p.slug for p in vivier.retenus] == ["legumes"]

    def test_un_ingredient_refuse_exclut(self):
        vivier = filtrer(
            [plat("avec-oignon", ingredients=frozenset({"oignon"})), plat("sans")],
            ContraintesUtilisateur(
                goal=Goal.BALANCED_DIET,
                kcal_target=Decimal("2000"),
                disliked_ingredients=frozenset({"oignon"}),
            ),
        )

        assert [p.slug for p in vivier.retenus] == ["sans"]

    def test_les_preferences_n_excluent_pas(self):
        """Un plat non préféré reste sélectionnable : il passe simplement après."""
        vivier = filtrer(
            [plat("neutre")],
            ContraintesUtilisateur(
                goal=Goal.BALANCED_DIET,
                kcal_target=Decimal("2000"),
                preferred_ingredients=frozenset({"tomate"}),
            ),
        )

        assert vivier.taille == 1

    def test_chaque_exclusion_porte_son_motif(self):
        """Sans motif, un vivier vide est indébuggable."""
        vivier = filtrer(
            [plat("a", allergens=frozenset({Allergen.MILK}))],
            ContraintesUtilisateur(
                goal=Goal.BALANCED_DIET,
                kcal_target=Decimal("2000"),
                allergens=frozenset({Allergen.MILK}),
            ),
        )

        assert all(e.motif for e in vivier.exclus)


class TestScore:
    def test_l_adequation_calorique_prime(self):
        cible = Decimal("500")
        pile = scorer(plat("pile", kcal="500"), MealSlot.LUNCH, cible, CONTRAINTES)
        loin = scorer(plat("loin", kcal="1500"), MealSlot.LUNCH, cible, CONTRAINTES)

        assert pile.total > loin.total

    def test_un_plat_deja_utilise_est_penalise(self):
        p = plat("x")
        neuf = scorer(p, MealSlot.LUNCH, Decimal("500"), CONTRAINTES, deja_utilise=0)
        repris = scorer(p, MealSlot.LUNCH, Decimal("500"), CONTRAINTES, deja_utilise=2)

        assert repris.total < neuf.total

    def test_objectif_compatible_favorise(self):
        avec = scorer(
            plat("a", goals=frozenset({Goal.BALANCED_DIET})),
            MealSlot.LUNCH, Decimal("500"), CONTRAINTES,
        )
        autre = scorer(
            plat("b", goals=frozenset({Goal.MUSCLE_GAIN})),
            MealSlot.LUNCH, Decimal("500"), CONTRAINTES,
        )

        assert avec.total > autre.total

    def test_cout_inconnu_ni_favorise_ni_penalise(self):
        score = scorer(
            plat("sans-cout"), MealSlot.LUNCH, Decimal("500"), CONTRAINTES,
            budget_repas=Decimal("1000"),
        )

        assert score.detail["cost"] > 0

    def test_le_detail_est_expose(self):
        """FN-020 — on doit pouvoir expliquer pourquoi un plat a été retenu."""
        score = scorer(plat("x"), MealSlot.LUNCH, Decimal("500"), CONTRAINTES)

        assert {"nutrition", "cost", "preference", "variety", "favorite"} <= set(
            score.detail
        )


class TestVivierInsuffisant:
    def test_catalogue_vide(self):
        echec = composer(filtrer([], CONTRAINTES), CONTRAINTES, 3, REGLES)

        assert isinstance(echec, EchecGeneration)
        assert echec.code == "NO_COMPATIBLE_DISH"

    def test_vivier_sous_le_minimum_refuse(self):
        """Le cas le plus probable avec un catalogue jeune — et le plus tentant
        à contourner par un programme répétitif."""
        vivier = filtrer(catalogue(5), CONTRAINTES)
        echec = composer(vivier, CONTRAINTES, 3, REGLES)

        assert isinstance(echec, EchecGeneration)
        assert echec.code == "CATALOG_TOO_SMALL"
        assert echec.details["pool"] == 5
        assert echec.details["required"] == REGLES.min_pool_size

    def test_duree_hors_bornes(self):
        vivier = filtrer(catalogue(40), CONTRAINTES)

        assert composer(vivier, CONTRAINTES, 0, REGLES).code == "PERIOD_INVALID"
        assert composer(vivier, CONTRAINTES, 99, REGLES).code == "PERIOD_INVALID"

    def test_tout_filtre_par_allergie(self):
        contraintes = ContraintesUtilisateur(
            goal=Goal.BALANCED_DIET,
            kcal_target=Decimal("2000"),
            allergens=frozenset({Allergen.PEANUT}),
        )
        vivier = filtrer(
            catalogue(40, allergens=frozenset({Allergen.PEANUT})), contraintes
        )
        echec = composer(vivier, contraintes, 3, REGLES)

        assert isinstance(echec, EchecGeneration)
        assert echec.code == "NO_COMPATIBLE_DISH"


class TestComposition:
    def _programme(self, jours: int = 3, n: int = 60, **kwargs) -> ProgrammeGenere:
        vivier = filtrer(catalogue(n, **kwargs), CONTRAINTES)
        resultat = composer(vivier, CONTRAINTES, jours, REGLES, seed=42)
        assert isinstance(resultat, ProgrammeGenere)
        return resultat

    def test_tous_les_jours_sont_couverts(self):
        programme = self._programme(jours=5)

        assert len(programme.journees) == 5
        assert all(j.repas for j in programme.journees)

    def test_tous_les_creneaux_sont_servis(self):
        programme = self._programme(jours=1)
        slots = {r.slot for r in programme.journees[0].repas}

        assert slots == set(REGLES.meal_distribution)

    def test_pas_deux_fois_le_meme_plat_dans_une_journee(self):
        programme = self._programme(jours=3)
        for journee in programme.journees:
            slugs = [r.plat.slug for r in journee.repas]
            assert len(slugs) == len(set(slugs))

    def test_max_repeats_respecte(self):
        programme = self._programme(jours=7)
        compte: dict[str, int] = {}
        for journee in programme.journees:
            for repas in journee.repas:
                compte[repas.plat.slug] = compte.get(repas.plat.slug, 0) + 1

        assert max(compte.values()) <= REGLES.max_repeats

    def test_min_gap_days_respecte(self):
        programme = self._programme(jours=7)
        dernier: dict[str, int] = {}
        for journee in programme.journees:
            for repas in journee.repas:
                precedent = dernier.get(repas.plat.slug)
                if precedent is not None:
                    assert journee.index - precedent >= REGLES.min_gap_days
                dernier[repas.plat.slug] = journee.index

    def test_creneau_non_couvert_est_signale_pas_masque(self):
        """Un petit-déjeuner introuvable doit se voir, pas disparaître."""
        plats = catalogue(40, types=frozenset({"lunch", "dinner"}))
        vivier = filtrer(plats, CONTRAINTES)
        resultat = composer(vivier, CONTRAINTES, 2, REGLES, seed=1)

        assert isinstance(resultat, ProgrammeGenere)
        non_couverts = {slot for _, slot in resultat.creneaux_non_couverts}
        assert MealSlot.BREAKFAST in non_couverts

    def test_le_seed_rend_la_generation_reproductible(self):
        vivier = filtrer(catalogue(60), CONTRAINTES)
        a = composer(vivier, CONTRAINTES, 4, REGLES, seed=7)
        b = composer(vivier, CONTRAINTES, 4, REGLES, seed=7)

        assert isinstance(a, ProgrammeGenere) and isinstance(b, ProgrammeGenere)
        assert [r.plat.slug for j in a.journees for r in j.repas] == [
            r.plat.slug for j in b.journees for r in j.repas
        ]

    def test_le_seed_est_conserve(self):
        programme = self._programme()
        assert programme.seed == 42


class TestValidationFinale:
    def _programme(self) -> ProgrammeGenere:
        vivier = filtrer(catalogue(60), CONTRAINTES)
        resultat = composer(vivier, CONTRAINTES, 2, REGLES, seed=3)
        assert isinstance(resultat, ProgrammeGenere)
        return resultat

    def test_programme_sain_est_conforme(self):
        resultat = valider(self._programme(), CONTRAINTES, REGLES)

        assert resultat.incident_securite is False
        assert 1 not in resultat.controles_en_echec

    def test_un_allergene_est_un_incident_de_securite(self):
        """Le contrôle 1 rejoue le filtrage sur le programme **final** : le LLM
        intervient après la sélection, rien ne garantit qu'il ne l'ait pas
        déformée."""
        contraintes = ContraintesUtilisateur(
            goal=Goal.BALANCED_DIET,
            kcal_target=Decimal("2000"),
            allergens=frozenset({Allergen.PEANUT}),
        )
        # Un programme composé sans la contrainte, puis validé avec :
        # c'est exactement la recomposition hasardeuse qu'on veut attraper.
        programme = self._programme()
        object.__setattr__(
            programme.journees[0].repas[0].plat,
            "allergens",
            frozenset({Allergen.PEANUT}),
        )

        resultat = valider(programme, contraintes, REGLES)

        assert 1 in resultat.controles_en_echec
        assert resultat.incident_securite is True
        assert resultat.conforme is False

    def test_plat_hors_catalogue_est_un_incident(self):
        """D-06 — le LLM ne compose pas. Un plat qu'il aurait inventé est rejeté."""
        programme = self._programme()
        resultat = valider(
            programme, CONTRAINTES, REGLES, catalogue_autorise=frozenset({"inexistant"})
        )

        assert 4 in resultat.controles_en_echec
        assert resultat.incident_securite is True
        assert resultat.details["plats_hors_catalogue"]

    def test_ecart_calorique_n_est_pas_un_incident_de_securite(self):
        """Un programme un peu trop calorique est un défaut de qualité, pas un
        danger : il ne doit pas déclencher l'alerte sécurité."""
        contraintes = ContraintesUtilisateur(
            goal=Goal.BALANCED_DIET, kcal_target=Decimal("10")
        )
        resultat = valider(self._programme(), contraintes, REGLES)

        assert 5 in resultat.controles_en_echec
        assert resultat.incident_securite is False


class TestRepliSansLLM:
    def test_la_justification_de_repli_decrit_le_programme(self):
        vivier = filtrer(catalogue(60), CONTRAINTES)
        programme = composer(vivier, CONTRAINTES, 2, REGLES, seed=5)
        assert isinstance(programme, ProgrammeGenere)

        texte = justification_de_repli(programme, CONTRAINTES)

        assert "2 jour" in texte
        assert str(int(CONTRAINTES.kcal_target)) in texte
