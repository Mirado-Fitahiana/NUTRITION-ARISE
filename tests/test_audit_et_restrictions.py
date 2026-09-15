"""Journal d'audit (FN-036) et normalisation des restrictions (FN-004).

Deux règles proches par leur forme : dans les deux cas, l'erreur consiste à
laisser passer discrètement quelque chose qui aurait dû être signalé.

* le journal ne doit **jamais** contenir de donnée de santé ni de secret ;
* une restriction non normalisable ne doit **jamais** être appliquée
  silencieusement.
"""

import pytest

from app.models.enums import RestrictionType
from app.services.audit import FORBIDDEN_KEYS, REDACTED, sanitize
from app.services.restrictions import (
    IngredientIndex,
    fold,
    normalize,
    split_terms,
)

RIZ = "11111111-1111-1111-1111-111111111111"
BREDE = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def index() -> IngredientIndex:
    import uuid

    return IngredientIndex.build(
        [
            (uuid.UUID(RIZ), "Riz blanc (cru)", "riz-blanc-cru", ["Vary"]),
            (uuid.UUID(BREDE), "Brède mafana", "brede-mafana", ["Anamalaho"]),
        ]
    )


# --------------------------------------------------------------------------
# FN-036 — journal
# --------------------------------------------------------------------------


class TestMasquage:
    @pytest.mark.parametrize(
        "cle", ["weight_kg", "height_cm", "birth_date", "allergens", "city"]
    )
    def test_les_donnees_de_sante_sont_masquees(self, cle):
        """§4.6 — le fait qu'une donnée ait changé est utile au débogage ; sa
        valeur ne l'est pas."""
        result = sanitize({cle: "valeur sensible"})
        assert result[cle] == REDACTED

    @pytest.mark.parametrize("cle", ["password", "token", "api_key", "prompt"])
    def test_les_secrets_sont_masques(self, cle):
        assert sanitize({cle: "s3cr3t"})[cle] == REDACTED

    def test_la_cle_est_conservee(self):
        """Retirer la clé laisserait croire que l'information n'existait pas."""
        assert "weight_kg" in sanitize({"weight_kg": 68})

    def test_le_masquage_est_recursif(self):
        details = {"changement": {"profil": {"weight_kg": 68, "goal": "weight_loss"}}}
        result = sanitize(details)
        assert result["changement"]["profil"]["weight_kg"] == REDACTED
        # `goal` n'est pas une donnée de santé : il reste lisible.
        assert result["changement"]["profil"]["goal"] == "weight_loss"

    def test_les_listes_sont_traversees(self):
        result = sanitize({"entrees": [{"token": "abc"}, {"action": "login"}]})
        assert result["entrees"][0]["token"] == REDACTED
        assert result["entrees"][1]["action"] == "login"

    def test_ce_qui_est_utile_passe(self):
        details = {"changed_fields": ["weight_kg"], "plans_marked_obsolete": 2}
        result = sanitize(details)
        # Le *nom* du champ modifié est une donnée technique, pas de santé.
        assert result["changed_fields"] == ["weight_kg"]
        assert result["plans_marked_obsolete"] == 2

    def test_la_liste_couvre_les_deux_familles(self):
        assert {"weight_kg", "allergens"} <= FORBIDDEN_KEYS
        assert {"password", "token", "open_router_key"} <= FORBIDDEN_KEYS

    def test_un_journal_vide_reste_vide(self):
        assert sanitize(None) == {}


# --------------------------------------------------------------------------
# FN-004 — normalisation
# --------------------------------------------------------------------------


class TestNormalisation:
    def test_une_restriction_structuree_est_normalisee_d_office(self, index):
        result = normalize(RestrictionType.NO_PORK, None, index)
        assert result.is_normalized
        assert not result.needs_admin_review
        assert result.tags == ("no_pork",)

    def test_un_libelle_connu_devient_un_tag(self, index):
        result = normalize(RestrictionType.CUSTOM, "sans gluten", index)
        assert result.is_normalized
        assert result.tags == ("gluten_free",)

    def test_un_slug_du_catalogue_est_apparie(self, index):
        result = normalize(RestrictionType.FORBIDDEN_FOOD, "riz-blanc-cru", index)
        assert result.is_normalized
        assert str(result.ingredient_ids[0]) == RIZ

    def test_un_alias_est_apparie(self, index):
        """« Vary » est le nom malgache du riz : l'alias doit suffire."""
        result = normalize(RestrictionType.FORBIDDEN_FOOD, "Vary", index)
        assert result.is_normalized
        assert str(result.ingredient_ids[0]) == RIZ

    def test_un_terme_inconnu_part_en_revue(self, index):
        """La règle qui compte : signalée, **pas** appliquée."""
        result = normalize(RestrictionType.CUSTOM, "pas de wombat", index)
        assert not result.is_normalized
        assert result.needs_admin_review
        assert "wombat" in result.unmatched

    def test_un_appariement_partiel_part_en_revue(self, index):
        """Appliquer la moitié d'une restriction laisserait passer l'autre
        moitié — exactement l'application silencieuse que FN-004 interdit."""
        result = normalize(
            RestrictionType.FORBIDDEN_FOOD, "riz-blanc-cru et wombat", index
        )
        assert not result.is_normalized
        assert result.needs_admin_review
        assert result.unmatched == ("wombat",)

    def test_plusieurs_termes_tous_connus_sont_appliques(self, index):
        result = normalize(
            RestrictionType.FORBIDDEN_FOOD, "riz-blanc-cru, brede-mafana", index
        )
        assert result.is_normalized
        assert len(result.ingredient_ids) == 2

    def test_un_libelle_vide_part_en_revue(self, index):
        result = normalize(RestrictionType.CUSTOM, "   ", index)
        assert not result.is_normalized
        assert result.needs_admin_review


class TestOutillage:
    @pytest.mark.parametrize(
        ("brut", "attendu"),
        [("Brède  Mafana", "brede mafana"), ("SANS Gluten", "sans gluten")],
    )
    def test_le_repliage_ignore_accents_casse_et_espaces(self, brut, attendu):
        assert fold(brut) == attendu

    @pytest.mark.parametrize(
        ("libelle", "attendu"),
        [
            ("pas de crevettes", ["crevettes"]),
            ("crevettes, crabe", ["crevettes", "crabe"]),
            ("crevettes et crabe", ["crevettes", "crabe"]),
            ("sans porc ni alcool", ["porc", "alcool"]),
        ],
    )
    def test_le_decoupage_reste_explicite(self, libelle, attendu):
        """Aucun appariement flou : mieux vaut envoyer en revue qu'apparier de
        travers."""
        assert split_terms(libelle) == attendu
