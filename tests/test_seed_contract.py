"""Contrat de seed : ce que le chargeur doit refuser (FN-040, FN-008, FN-009).

Un fichier de seed mal formé doit échouer **au chargement**, pas produire une
donnée silencieusement fausse dans un programme alimentaire réel.

Le dernier test valide les fichiers réellement présents dans `seeds/` : il sert
de garde-fou d'intégration continue à chaque contribution au catalogue.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.enums import DishStatus
from app.seed.loader import SeedReport, _resolve_status, parse_dishes, parse_ingredients
from app.seed.schemas import SeedDish, SeedIngredient

SEEDS_DIR = Path(__file__).resolve().parents[1] / "seeds"


def ingredient_payload(**overrides) -> dict:
    payload = {
        "slug": "riz-blanc-cru",
        "name": "Riz blanc",
        "category": "cereals",
        "reference_unit": "g",
        "nutrition": {
            "source": "CIQUAL — riz blanc cru",
            "kcal_100": 349,
            "protein_100": 7.1,
            "carbs_100": 77.9,
            "fat_100": 0.9,
        },
    }
    payload.update(overrides)
    return payload


def dish_payload(**overrides) -> dict:
    payload = {
        "slug": "vary-amin-anana",
        "name": "Vary amin'anana",
        "meal_types": ["lunch"],
        "servings": 4,
        "ingredients": [{"ingredient": "riz-blanc-cru", "quantity": 300, "unit": "g"}],
    }
    payload.update(overrides)
    return payload


class TestIngredient:
    def test_entree_minimale_valide(self):
        assert SeedIngredient.model_validate(ingredient_payload()).slug == "riz-blanc-cru"

    def test_source_nutritionnelle_obligatoire(self):
        """« Ne pas inventer de valeurs » : sans source, l'entrée est refusée."""
        payload = ingredient_payload()
        del payload["nutrition"]["source"]
        with pytest.raises(ValidationError):
            SeedIngredient.model_validate(payload)

    def test_champ_inconnu_refuse(self):
        """Une faute de frappe ne doit pas devenir une donnée ignorée."""
        with pytest.raises(ValidationError):
            SeedIngredient.model_validate(ingredient_payload(kcal_100=349))

    def test_slug_invalide(self):
        with pytest.raises(ValidationError):
            SeedIngredient.model_validate(ingredient_payload(slug="Riz Blanc"))

    def test_verification_allergene_partielle_refusee(self):
        """🔴 Les trois champs de vérification vont ensemble, ou aucun."""
        payload = ingredient_payload(
            allergens={"values": [], "verified_by": "nutritionist:x"}
        )
        with pytest.raises(ValidationError, match="atomique"):
            SeedIngredient.model_validate(payload)

    def test_verification_allergene_complete_acceptee(self):
        payload = ingredient_payload(
            allergens={
                "values": ["peanut"],
                "source": "CIQUAL",
                "verified_by": "nutritionist:x",
                "verified_at": "2026-09-15",
            }
        )
        assert SeedIngredient.model_validate(payload).allergens.is_verified

    def test_allergene_inconnu_refuse(self):
        payload = ingredient_payload(allergens={"values": ["kiwi"]})
        with pytest.raises(ValidationError):
            SeedIngredient.model_validate(payload)

    def test_mois_de_saison_invalide(self):
        with pytest.raises(ValidationError, match="mois de saison"):
            SeedIngredient.model_validate(ingredient_payload(seasonality=[13]))

    def test_conversion_identite_refusee(self):
        payload = ingredient_payload(
            unit_conversions=[
                {"from_unit": "g", "to_unit": "g", "factor": 1, "source": "measured"}
            ]
        )
        with pytest.raises(ValidationError, match="différer"):
            SeedIngredient.model_validate(payload)


class TestPlat:
    def test_entree_minimale_valide(self):
        assert SeedDish.model_validate(dish_payload()).servings == 4

    def test_tag_derive_refuse_en_saisie(self):
        """FN-009 — `vegetarian` se calcule, il ne se déclare pas."""
        with pytest.raises(ValidationError, match="tags dérivés interdits"):
            SeedDish.model_validate(dish_payload(tags=["vegetarian", "local"]))

    def test_tag_subjectif_accepte(self):
        dish = SeedDish.model_validate(dish_payload(tags=["local", "family_meal"]))
        assert len(dish.tags) == 2

    def test_au_moins_un_ingredient(self):
        with pytest.raises(ValidationError):
            SeedDish.model_validate(dish_payload(ingredients=[]))

    def test_quantite_nulle_refusee(self):
        payload = dish_payload(
            ingredients=[{"ingredient": "riz-blanc-cru", "quantity": 0, "unit": "g"}]
        )
        with pytest.raises(ValidationError):
            SeedDish.model_validate(payload)

    def test_ingredient_en_double_refuse(self):
        payload = dish_payload(
            ingredients=[
                {"ingredient": "riz-blanc-cru", "quantity": 100, "unit": "g"},
                {"ingredient": "riz-blanc-cru", "quantity": 200, "unit": "g"},
            ]
        )
        with pytest.raises(ValidationError, match="double"):
            SeedDish.model_validate(payload)

    def test_au_moins_un_type_de_repas(self):
        with pytest.raises(ValidationError):
            SeedDish.model_validate(dish_payload(meal_types=[]))

    def test_publication_sans_validation_refusee(self):
        with pytest.raises(ValidationError, match="validation signée"):
            SeedDish.model_validate(dish_payload(validation={"publish": True}))

    def test_validateur_egal_auteur_refuse(self):
        """FN-008 — la validation exige un second regard."""
        payload = dish_payload(
            author="admin:paul",
            validation={
                "validated_by": "admin:paul",
                "validated_at": "2026-09-20",
                "publish": True,
            },
        )
        with pytest.raises(ValidationError, match="validateur"):
            SeedDish.model_validate(payload)


class TestResolutionDuStatut:
    """🔴 FN-003 — le garde-fou de publication du chargeur."""

    def _dish(self, **validation) -> SeedDish:
        return SeedDish.model_validate(dish_payload(validation=validation))

    def test_sans_demande_de_publication_reste_brouillon(self):
        report = SeedReport()
        status = _resolve_status(self._dish(), unverified=(), report=report)
        assert status is DishStatus.DRAFT

    def test_valide_mais_non_publie(self):
        report = SeedReport()
        dish = self._dish(validated_by="nutritionist:x", validated_at="2026-09-20")
        assert _resolve_status(dish, (), report) is DishStatus.VALIDATED

    def test_publication_accordee_si_tout_est_verifie(self):
        report = SeedReport()
        dish = self._dish(
            validated_by="nutritionist:x", validated_at="2026-09-20", publish=True
        )
        assert _resolve_status(dish, (), report) is DishStatus.PUBLISHED
        assert report.dishes_published == 1
        assert report.publication_blocked == []

    def test_publication_refusee_sur_ingredient_non_verifie(self):
        report = SeedReport()
        dish = self._dish(
            validated_by="nutritionist:x", validated_at="2026-09-20", publish=True
        )
        status = _resolve_status(dish, ("brede-mafana",), report)

        assert status is DishStatus.PENDING_VALIDATION
        assert report.dishes_published == 0
        assert len(report.publication_blocked) == 1
        slug, reason = report.publication_blocked[0]
        assert slug == "vary-amin-anana"
        assert "brede-mafana" in reason


class TestFichiersDuDepot:
    """Garde-fou d'intégration continue sur le catalogue réel."""

    def test_les_ingredients_du_depot_sont_valides(self):
        _, errors = parse_ingredients(SEEDS_DIR / "ingredients")
        assert errors == []

    def test_les_plats_du_depot_sont_valides(self):
        _, errors = parse_dishes(SEEDS_DIR / "dishes")
        assert errors == []

    def test_tout_plat_reference_un_ingredient_connu(self):
        ingredients, _ = parse_ingredients(SEEDS_DIR / "ingredients")
        dishes, _ = parse_dishes(SEEDS_DIR / "dishes")
        for dish in dishes.values():
            for entry in dish.ingredients:
                assert entry.ingredient in ingredients, (
                    f"{dish.slug} référence l'ingrédient inconnu {entry.ingredient}"
                )
