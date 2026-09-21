"""Couverture du catalogue (sprint 03, §2 et §5).

Ce que ces tests tiennent : **un catalogue volumineux n'est pas un catalogue
couvrant**. Compter les plats rassure ; croiser objectif × restriction × repas
dit la vérité. Si la règle de croisement se relâche un jour, un utilisateur
végétarien en prise de masse se retrouverait sans aucun résultat sans qu'aucun
compteur ne bouge — c'est précisément ce que le sprint 03 demande d'empêcher.

Aucune base n'est nécessaire : les plats sont construits en mémoire.
"""

from app.models.enums import DishStatus, Goal, MealType, RestrictionType
from app.services.couverture import (
    CIBLE_INGREDIENTS,
    CIBLE_PLATS_PUBLIES,
    POOL_MINIMAL_PAR_CRENEAU,
    IngredientResume,
    PlatResume,
    analyser,
)


def plat(
    slug: str,
    *,
    status: DishStatus = DishStatus.PUBLISHED,
    meal_types: tuple[str, ...] = (MealType.LUNCH.value,),
    goals: tuple[str, ...] = (Goal.BALANCED_DIET.value,),
    restrictions: tuple[str, ...] = (),
) -> PlatResume:
    return PlatResume(
        slug=slug,
        status=status,
        meal_types=meal_types,
        compatible_goals=goals,
        compatible_restrictions=restrictions,
    )


def ingredient(slug: str, *, verifie: bool = True) -> IngredientResume:
    return IngredientResume(slug=slug, allergenes_verifies=verifie)


class TestAvancement:
    def test_catalogue_vide_ne_couvre_rien(self):
        rapport = analyser([], [])

        assert rapport.plats_publies == 0
        assert rapport.pret_pour_le_lot_2 is False
        assert len(rapport.creneaux_vides) == len(rapport.creneaux)

    def test_seuls_les_plats_publies_comptent(self):
        """Un plat en brouillon n'est pas recommandable : le faire figurer
        donnerait une image flatteuse et fausse de l'avancement."""
        rapport = analyser(
            [
                plat("publie"),
                plat("brouillon", status=DishStatus.DRAFT),
                plat("en-attente", status=DishStatus.PENDING_VALIDATION),
            ],
            [],
        )

        assert rapport.plats_publies == 1
        assert rapport.plats_par_statut[DishStatus.DRAFT.value] == 1
        assert rapport.plats_par_statut[DishStatus.PENDING_VALIDATION.value] == 1

    def test_ingredients_non_signes_sont_comptes_a_part(self):
        rapport = analyser(
            [],
            [ingredient("riz"), ingredient("brede", verifie=False)],
        )

        assert rapport.ingredients_total == 2
        assert rapport.ingredients_verifies == 1


class TestCreneaux:
    def test_un_plat_couvre_son_objectif_et_son_repas(self):
        rapport = analyser(
            [plat("p1", meal_types=(MealType.BREAKFAST.value,), goals=(Goal.WEIGHT_LOSS.value,))],
            [],
        )

        cible = next(
            c
            for c in rapport.creneaux
            if c.objectif == Goal.WEIGHT_LOSS
            and c.restriction is None
            and c.type_repas == MealType.BREAKFAST
        )
        assert cible.plats == 1
        assert cible.vide is False

    def test_un_plat_ne_couvre_pas_un_autre_objectif(self):
        rapport = analyser(
            [plat("p1", goals=(Goal.WEIGHT_LOSS.value,))],
            [],
        )

        autre = next(
            c
            for c in rapport.creneaux
            if c.objectif == Goal.MUSCLE_GAIN
            and c.restriction is None
            and c.type_repas == MealType.LUNCH
        )
        assert autre.vide is True

    def test_un_plat_sans_restriction_ne_couvre_pas_les_vegetariens(self):
        """Le piège que le sprint 03 vise : 180 plats omnivores laissent un
        utilisateur végétarien sans aucun résultat."""
        rapport = analyser([plat("poulet-riz", restrictions=())], [])

        vegetarien = next(
            c
            for c in rapport.creneaux
            if c.objectif == Goal.BALANCED_DIET
            and c.restriction == RestrictionType.VEGETARIAN
            and c.type_repas == MealType.LUNCH
        )
        sans_restriction = next(
            c
            for c in rapport.creneaux
            if c.objectif == Goal.BALANCED_DIET
            and c.restriction is None
            and c.type_repas == MealType.LUNCH
        )

        assert sans_restriction.plats == 1
        assert vegetarien.vide is True

    def test_un_plat_vegetarien_couvre_les_deux(self):
        rapport = analyser(
            [plat("vary-anana", restrictions=(RestrictionType.VEGETARIAN.value,))],
            [],
        )

        vegetarien = next(
            c
            for c in rapport.creneaux
            if c.objectif == Goal.BALANCED_DIET
            and c.restriction == RestrictionType.VEGETARIAN
            and c.type_repas == MealType.LUNCH
        )
        assert vegetarien.plats == 1

    def test_pool_en_dessous_du_minimum_est_signale_sans_etre_vide(self):
        plats = [plat(f"p{i}") for i in range(POOL_MINIMAL_PAR_CRENEAU - 1)]
        rapport = analyser(plats, [])

        creneau = next(
            c
            for c in rapport.creneaux
            if c.objectif == Goal.BALANCED_DIET
            and c.restriction is None
            and c.type_repas == MealType.LUNCH
        )
        assert creneau.vide is False
        assert creneau.insuffisant is True
        assert creneau in rapport.creneaux_insuffisants
        assert creneau not in rapport.creneaux_vides


class TestSeuilDuLot2:
    def _catalogue_complet(self) -> tuple[list[PlatResume], list[IngredientResume]]:
        """Un catalogue qui couvre tous les créneaux évalués, au pool minimal."""
        restrictions = tuple(r.value for r in RestrictionType)
        plats = [
            plat(
                f"p{i}",
                meal_types=tuple(m.value for m in MealType),
                goals=tuple(g.value for g in Goal),
                restrictions=restrictions,
            )
            for i in range(CIBLE_PLATS_PUBLIES)
        ]
        ingredients = [ingredient(f"i{i}") for i in range(CIBLE_INGREDIENTS)]
        return plats, ingredients

    def test_catalogue_complet_debloque_le_lot_2(self):
        plats, ingredients = self._catalogue_complet()
        rapport = analyser(plats, ingredients)

        assert rapport.creneaux_vides == []
        assert rapport.pret_pour_le_lot_2 is True

    def test_un_seul_ingredient_non_signe_bloque_tout(self):
        """FN-003 : un ingrédient non signé rend non publiable tout plat qui
        l'emploie. Le seuil ne peut donc pas être atteint."""
        plats, ingredients = self._catalogue_complet()
        ingredients[0] = ingredient("i0", verifie=False)

        rapport = analyser(plats, ingredients)

        assert rapport.pret_pour_le_lot_2 is False

    def test_volume_atteint_mais_trou_de_couverture_bloque(self):
        """Le cœur du sprint 03 : atteindre 180 plats ne suffit pas si une
        combinaison fréquente reste à zéro."""
        plats, ingredients = self._catalogue_complet()
        plats = [
            PlatResume(
                slug=p.slug,
                status=p.status,
                meal_types=p.meal_types,
                compatible_goals=p.compatible_goals,
                compatible_restrictions=(),  # plus aucun plat végétarien
            )
            for p in plats
        ]

        rapport = analyser(plats, ingredients)

        assert rapport.plats_publies >= CIBLE_PLATS_PUBLIES
        assert rapport.creneaux_vides != []
        assert rapport.pret_pour_le_lot_2 is False
