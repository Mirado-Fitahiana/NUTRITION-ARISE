"""Calculs dérivés du catalogue — 🔴 dont les contrôles de sécurité alimentaire.

Ces tests couvrent la partie « catalogue » des tests obligatoires §14.1 de la
spécification : propagation des allergènes, y compris indirecte, et blocage de
la publication sur ingrédient non vérifié.

Ils sont volontairement sans base de données : ils doivent tourner en quelques
millisecondes, à chaque commit, sans excuse pour être désactivés.
"""

from decimal import Decimal

import pytest

from app.models.enums import (
    Allergen,
    DishTag,
    ReferenceUnit,
    RestrictionFlag,
    RestrictionType,
)
from app.services.nutrition import (
    DishComponent,
    IngredientFacts,
    compute_nutrition,
    derive_compatible_restrictions,
    derive_dish_data,
    propagate_allergens,
    unverified_ingredients,
)
from app.services.units import UnitConversionError


def d(value: str) -> Decimal:
    return Decimal(value)


def ingredient(
    slug: str,
    *,
    kcal: str = "0",
    protein: str = "0",
    carbs: str = "0",
    fat: str = "0",
    fiber: str = "0",
    allergens: set[Allergen] | None = None,
    flags: set[RestrictionFlag] | None = None,
    verified: bool = True,
    unit: ReferenceUnit = ReferenceUnit.G,
    density: str | None = None,
) -> IngredientFacts:
    return IngredientFacts(
        slug=slug,
        reference_unit=unit,
        kcal_100=d(kcal),
        protein_100=d(protein),
        carbs_100=d(carbs),
        fat_100=d(fat),
        fiber_100=d(fiber),
        allergens=frozenset(allergens or set()),
        restriction_flags=frozenset(flags or set()),
        density_g_per_ml=d(density) if density else None,
        is_allergen_verified=verified,
    )


VEGE = {RestrictionFlag.VEGETARIAN, RestrictionFlag.VEGAN}


class TestCalculNutritionnel:
    """D-11 — les valeurs sont calculées, jamais saisies."""

    def test_formule_de_base(self):
        riz = ingredient("riz", kcal="349", protein="7.1", carbs="77.9", fat="0.9")
        # 300 g pour 4 portions → 75 g par portion → 261,75 kcal
        facts = compute_nutrition([DishComponent(riz, d("300"), "g")], servings=4)
        assert facts.kcal == d("261.75")
        assert facts.protein_g == d("5.33")

    def test_somme_de_plusieurs_ingredients(self):
        riz = ingredient("riz", kcal="349")
        huile = ingredient("huile", kcal="828")
        facts = compute_nutrition(
            [
                DishComponent(riz, d("200"), "g"),
                DishComponent(huile, d("100"), "g"),
            ],
            servings=1,
        )
        assert facts.kcal == d("1526.00")  # 698 + 828

    def test_conversion_appliquee_avant_calcul(self):
        riz = ingredient("riz", kcal="349")
        facts = compute_nutrition([DishComponent(riz, d("0.3"), "kg")], servings=1)
        assert facts.kcal == d("1047.00")

    def test_conversion_manquante_interrompt_le_calcul(self):
        """FN-012 — jamais de valeur approchée en repli."""
        riz = ingredient("riz", kcal="349")
        with pytest.raises(UnitConversionError):
            compute_nutrition([DishComponent(riz, d("1"), "kapoaka")], servings=1)

    def test_servings_invalide(self):
        riz = ingredient("riz", kcal="349")
        with pytest.raises(ValueError):
            compute_nutrition([DishComponent(riz, d("100"), "g")], servings=0)


class TestPropagationAllergenes:
    """🔴 §14.1 — sécurité alimentaire."""

    def test_union_des_allergenes(self):
        components = [
            DishComponent(ingredient("huile", allergens={Allergen.PEANUT}), d("10"), "g"),
            DishComponent(ingredient("lait", allergens={Allergen.MILK}), d("50"), "g"),
            DishComponent(ingredient("riz"), d("100"), "g"),
        ]
        assert propagate_allergens(components) == {Allergen.PEANUT, Allergen.MILK}

    def test_allergene_indirect_est_propage(self):
        """La source d'erreur la plus probable : un allergène porté par un
        ingrédient composé, invisible dans le nom du plat."""
        sauce = ingredient("sauce-composee", allergens={Allergen.SOY, Allergen.GLUTEN})
        components = [DishComponent(sauce, d("30"), "g")]
        assert Allergen.SOY in propagate_allergens(components)
        assert Allergen.GLUTEN in propagate_allergens(components)

    def test_aucun_allergene_donne_ensemble_vide(self):
        components = [DishComponent(ingredient("riz"), d("100"), "g")]
        assert propagate_allergens(components) == frozenset()

    def test_un_plat_sans_allergene_declare_reste_sans_allergene(self):
        """Un allergène ne peut jamais apparaître de nulle part : il vient
        toujours d'un ingrédient."""
        derived = derive_dish_data(
            [DishComponent(ingredient("riz", kcal="349"), d("100"), "g")], servings=1
        )
        assert derived.allergens == frozenset()


class TestBlocagePublication:
    """🔴 FN-003 — un ingrédient non vérifié rend le plat non publiable."""

    def test_ingredient_non_verifie_est_signale(self):
        components = [
            DishComponent(ingredient("riz", verified=True), d("100"), "g"),
            DishComponent(ingredient("brede", verified=False), d("100"), "g"),
        ]
        assert unverified_ingredients(components) == ("brede",)

    def test_tous_verifies_ne_bloque_pas(self):
        components = [DishComponent(ingredient("riz", verified=True), d("100"), "g")]
        assert unverified_ingredients(components) == ()

    def test_absence_d_allergene_ne_vaut_pas_verification(self):
        """Un ingrédient sans allergène déclaré mais non signé reste bloquant :
        l'absence d'allergène est une affirmation, pas une donnée manquante."""
        sans_allergene_non_signe = ingredient("riz", allergens=set(), verified=False)
        components = [DishComponent(sans_allergene_non_signe, d("100"), "g")]
        assert unverified_ingredients(components) == ("riz",)


class TestRestrictionsDerivees:
    def test_vegetarien_exige_tous_les_ingredients(self):
        components = [
            DishComponent(ingredient("riz", flags=VEGE), d("100"), "g"),
            DishComponent(ingredient("brede", flags=VEGE), d("100"), "g"),
        ]
        assert RestrictionType.VEGETARIAN in derive_compatible_restrictions(components)

    def test_un_seul_ingredient_carne_suffit_a_exclure(self):
        components = [
            DishComponent(ingredient("riz", flags=VEGE), d("100"), "g"),
            DishComponent(ingredient("zebu", flags=set()), d("100"), "g"),
        ]
        satisfied = derive_compatible_restrictions(components)
        assert RestrictionType.VEGETARIAN not in satisfied
        assert RestrictionType.VEGAN not in satisfied

    def test_sans_lactose_si_aucun_ingredient_n_en_contient(self):
        components = [DishComponent(ingredient("riz", flags=VEGE), d("100"), "g")]
        assert RestrictionType.LACTOSE_FREE in derive_compatible_restrictions(components)

    def test_avec_lactose_exclut_sans_lactose(self):
        lait = ingredient("lait", flags={RestrictionFlag.LACTOSE})
        components = [DishComponent(lait, d("100"), "g")]
        assert RestrictionType.LACTOSE_FREE not in derive_compatible_restrictions(
            components
        )

    def test_allergene_gluten_exclut_sans_gluten(self):
        """Même sans le drapeau `gluten`, l'allergène déclaré suffit à exclure."""
        ble = ingredient("ble", allergens={Allergen.GLUTEN})
        components = [DishComponent(ble, d("100"), "g")]
        assert RestrictionType.GLUTEN_FREE not in derive_compatible_restrictions(
            components
        )

    def test_plat_sans_ingredient_ne_satisfait_rien(self):
        assert derive_compatible_restrictions([]) == frozenset()


class TestTagsDerives:
    def test_high_protein_au_dela_du_seuil(self):
        # 30 g de protéines pour 400 kcal → 30 % de l'énergie
        viande = ingredient("zebu", kcal="400", protein="30")
        derived = derive_dish_data([DishComponent(viande, d("100"), "g")], servings=1)
        assert DishTag.HIGH_PROTEIN in derived.derived_tags

    def test_pas_high_protein_en_dessous_du_seuil(self):
        riz = ingredient("riz", kcal="349", protein="7.1", carbs="77.9")
        derived = derive_dish_data([DishComponent(riz, d("100"), "g")], servings=1)
        assert DishTag.HIGH_PROTEIN not in derived.derived_tags

    def test_low_carb(self):
        viande = ingredient("zebu", kcal="400", protein="30", carbs="5")
        derived = derive_dish_data([DishComponent(viande, d("100"), "g")], servings=1)
        assert DishTag.LOW_CARB in derived.derived_tags

    def test_tags_de_regime_suivent_les_restrictions(self):
        riz = ingredient("riz", kcal="349", flags=VEGE)
        derived = derive_dish_data([DishComponent(riz, d("100"), "g")], servings=1)
        assert {DishTag.VEGETARIAN, DishTag.VEGAN} <= derived.derived_tags
