"""Règles du catalogue : cycle de vie, publication, contrat de saisie
(FN-003, FN-008, FN-009, D-11).

Ces tests portent sur les deux endroits où une erreur devient un incident :

* **ce qu'on peut saisir.** Si un schéma d'entrée accepte une valeur
  nutritionnelle ou un allergène, D-11 et FN-008 ne sont plus garantis par le
  code mais par la discipline des appelants — donc plus garantis du tout ;
* **quand on peut publier.** La table des transitions et le contrôle de
  publication sont les seuls remparts avant qu'un plat n'atteigne un programme.

Aucune base n'est nécessaire : la table de transitions est pure, et les
contrôles de publication opèrent sur des objets construits en mémoire.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.catalog import Dish, DishIngredient, Ingredient
from app.models.enums import DERIVED_TAGS, DishStatus, DishTag
from app.schemas.catalog import DishIn, IngredientIn, VerifyAllergensIn
from app.services.catalog import ALLOWED_TRANSITIONS, can_transition, publication_blockers


def dish_payload(**overrides) -> dict:
    base = {
        "slug": "plat-test",
        "name": "Plat de test",
        "meal_types": ["lunch"],
        "servings": 2,
        "ingredients": [{"ingredient": "riz-blanc-cru", "quantity": 200, "unit": "g"}],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Contrat de saisie
# --------------------------------------------------------------------------


class TestCeQuiNeSeSaisitPas:
    @pytest.mark.parametrize(
        "champ",
        ["kcal_portion", "protein_portion", "allergens", "compatible_restrictions"],
    )
    def test_les_valeurs_derivees_sont_refusees(self, champ):
        """D-11 · FN-008 — elles sont calculées depuis les ingrédients.
        `extra="forbid"` transforme la tentative en refus explicite."""
        with pytest.raises(ValidationError):
            DishIn.model_validate(dish_payload(**{champ: 100}))

    @pytest.mark.parametrize("champ", ["status", "validated_by", "published_at"])
    def test_le_cycle_de_vie_n_est_pas_saisissable(self, champ):
        """Un `PUT` capable de publier contournerait le contrôle FN-003."""
        with pytest.raises(ValidationError):
            DishIn.model_validate(dish_payload(**{champ: "published"}))

    @pytest.mark.parametrize("tag", sorted(t.value for t in DERIVED_TAGS))
    def test_les_tags_derivables_sont_refuses(self, tag):
        """FN-009 — seuls les tags subjectifs se saisissent."""
        with pytest.raises(ValidationError, match="calculés"):
            DishIn.model_validate(dish_payload(tags=[tag]))

    def test_les_tags_subjectifs_sont_acceptes(self):
        payload = DishIn.model_validate(
            dish_payload(tags=["quick", "family_meal", "local"])
        )
        assert DishTag.QUICK in payload.tags

    def test_un_plat_sans_ingredient_est_refuse(self):
        with pytest.raises(ValidationError):
            DishIn.model_validate(dish_payload(ingredients=[]))

    def test_un_ingredient_ne_figure_qu_une_fois(self):
        with pytest.raises(ValidationError, match="qu'une fois"):
            DishIn.model_validate(
                dish_payload(
                    ingredients=[
                        {"ingredient": "riz-blanc-cru", "quantity": 100, "unit": "g"},
                        {"ingredient": "riz-blanc-cru", "quantity": 50, "unit": "g"},
                    ]
                )
            )

    def test_une_quantite_nulle_est_refusee(self):
        with pytest.raises(ValidationError):
            DishIn.model_validate(
                dish_payload(
                    ingredients=[
                        {"ingredient": "riz-blanc-cru", "quantity": 0, "unit": "g"}
                    ]
                )
            )


class TestContratIngredient:
    def test_la_source_nutritionnelle_est_obligatoire(self):
        """FN-007 — aucune valeur ne doit être inventée."""
        with pytest.raises(ValidationError):
            IngredientIn.model_validate(
                {
                    "slug": "test", "name": "Test", "category": "cereals",
                    "reference_unit": "g", "kcal_100": 100, "protein_100": 1,
                    "carbs_100": 1, "fat_100": 1,
                }
            )

    def test_les_allergenes_ne_se_saisissent_pas_a_la_creation(self):
        """FN-003 — la signature est un acte distinct, réservé au
        nutritionniste. Les accepter ici les rendrait déclaratifs."""
        assert "allergens" not in IngredientIn.model_fields
        assert "allergen_verified_by" not in IngredientIn.model_fields

    def test_la_signature_exige_une_source(self):
        """« Aucun allergène » est une affirmation, pas une absence de donnée :
        elle n'a de valeur que sourcée."""
        with pytest.raises(ValidationError):
            VerifyAllergensIn.model_validate({"allergens": []})
        signed = VerifyAllergensIn.model_validate(
            {"allergens": [], "source": "CIQUAL — fiche vérifiée"}
        )
        assert signed.allergens == []

    def test_le_validateur_ne_se_declare_pas(self):
        """Il vient du jeton : la traçabilité de responsabilité ne peut pas
        reposer sur du déclaratif."""
        assert "verified_by" not in VerifyAllergensIn.model_fields

    def test_les_mois_de_saison_sont_bornes(self):
        with pytest.raises(ValidationError):
            IngredientIn.model_validate(
                {
                    "slug": "test", "name": "Test", "category": "vegetables",
                    "reference_unit": "g", "kcal_100": 10, "protein_100": 1,
                    "carbs_100": 1, "fat_100": 1,
                    "nutrition_source": "FAO/INFOODS", "seasonality": [1, 13],
                }
            )


# --------------------------------------------------------------------------
# Cycle de vie
# --------------------------------------------------------------------------


class TestTransitions:
    def test_un_brouillon_ne_se_publie_pas_directement(self):
        assert not can_transition(DishStatus.DRAFT, DishStatus.PUBLISHED)

    def test_le_parcours_nominal(self):
        chemin = [
            DishStatus.DRAFT,
            DishStatus.PENDING_VALIDATION,
            DishStatus.VALIDATED,
            DishStatus.PUBLISHED,
            DishStatus.ARCHIVED,
        ]
        for depart, arrivee in zip(chemin, chemin[1:]):
            assert can_transition(depart, arrivee), f"{depart} → {arrivee}"

    def test_un_plat_archive_le_reste(self):
        """Un plat a pu être recommandé ; l'historique s'appuie sur le figement,
        pas sur une résurrection."""
        assert ALLOWED_TRANSITIONS[DishStatus.ARCHIVED] == frozenset()
        for cible in DishStatus:
            assert not can_transition(DishStatus.ARCHIVED, cible)

    def test_un_plat_publie_ne_va_qu_aux_archives(self):
        assert ALLOWED_TRANSITIONS[DishStatus.PUBLISHED] == frozenset(
            {DishStatus.ARCHIVED}
        )

    def test_un_refus_peut_repartir_en_brouillon(self):
        assert can_transition(DishStatus.REJECTED, DishStatus.DRAFT)


# --------------------------------------------------------------------------
# Contrôle de publication
# --------------------------------------------------------------------------


def build_dish(*, verified: bool, validated: bool, author="admin-1") -> Dish:
    """Plat en mémoire, jamais ajouté à une session."""
    ingredient = Ingredient(
        slug="riz-blanc-cru",
        name="Riz blanc",
        category="cereals",
        reference_unit="g",
        kcal_100=Decimal("349"),
        protein_100=Decimal("7.1"),
        carbs_100=Decimal("77.9"),
        fat_100=Decimal("0.9"),
        fiber_100=Decimal("1.4"),
        nutrition_source="CIQUAL",
        allergens=[],
        restriction_flags=["vegetarian", "vegan"],
        seasonality=[],
    )
    ingredient.unit_conversions = []
    if verified:
        ingredient.allergen_source = "CIQUAL"
        ingredient.allergen_verified_by = "nutritionniste-1"
        ingredient.allergen_verified_at = datetime.now(UTC)

    dish = Dish(
        slug="plat-test",
        name="Plat de test",
        meal_types=["lunch"],
        servings=2,
        status=DishStatus.VALIDATED,
        author_id=author,
        compatible_goals=[],
        compatible_restrictions=[],
        nutrition_computed_at=datetime.now(UTC),
    )
    dish.ingredients = [
        DishIngredient(
            ingredient=ingredient, quantity=Decimal("200"), unit="g", display_order=0
        )
    ]
    dish.steps, dish.tags, dish.allergens = [], [], []
    if validated:
        dish.validated_by = "nutritionniste-1"
        dish.validated_at = datetime.now(UTC)
    return dish


class TestPublication:
    def test_un_ingredient_non_signe_bloque(self):
        """FN-003 — le blocage massif au démarrage est voulu."""
        blockers = publication_blockers(build_dish(verified=False, validated=True))
        assert any("allergènes non vérifiés" in b for b in blockers)

    def test_un_plat_non_valide_bloque(self):
        blockers = publication_blockers(build_dish(verified=True, validated=False))
        assert any("validé par un nutritionniste" in b for b in blockers)

    def test_le_validateur_ne_peut_pas_etre_l_auteur(self):
        dish = build_dish(verified=True, validated=True, author="nutritionniste-1")
        blockers = publication_blockers(dish)
        assert any("validateur ne peut pas être l'auteur" in b for b in blockers)

    def test_un_plat_sans_ingredient_bloque_en_premier(self):
        """L'ordre des refus compte : « aucun ingrédient » d'abord, sans quoi le
        message parlerait d'allergènes non vérifiés, ce qui serait vrai mais
        inutile."""
        dish = build_dish(verified=True, validated=True)
        dish.ingredients = []
        assert publication_blockers(dish) == ["le plat ne contient aucun ingrédient"]

    def test_un_plat_conforme_ne_bloque_rien(self):
        assert publication_blockers(build_dish(verified=True, validated=True)) == []
