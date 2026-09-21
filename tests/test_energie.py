"""Besoins énergétiques (FN-038).

Ce que ces tests tiennent est d'une autre nature que le reste : **un plancher
de sécurité qui cède est un problème de santé**, pas un défaut d'arrondi. Un
logiciel qui propose 900 kcal par jour à quelqu'un qui perd du poids ne produit
pas une erreur visible — il produit un programme crédible et dangereux.

La spec exige un test automatisé explicite : *« un programme prise de masse
apporte mesurablement plus de calories qu'un programme perte de poids pour un
même profil »*. Il est ci-dessous.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.enums import ActivityLevel, Goal, Sex
from app.services.energie import (
    FACTEUR_ACTIVITE,
    PLANCHER_ABSOLU,
    PLANCHER_LIPIDES_G_PAR_KG,
    age,
    bmr,
    calculer,
    tdee,
)

AUJOURDHUI = date(2026, 9, 15)


def besoins(**surcharges):
    base = dict(
        poids_kg=Decimal("70"),
        taille_cm=Decimal("175"),
        birth_date=date(1995, 1, 1),
        sexe=Sex.MALE,
        niveau_activite=ActivityLevel.MODERATELY_ACTIVE,
        objectif=Goal.WEIGHT_MAINTENANCE,
        aujourdhui=AUJOURDHUI,
    )
    return calculer(**{**base, **surcharges})


class TestAge:
    def test_anniversaire_passe(self):
        assert age(date(1995, 1, 1), aujourdhui=AUJOURDHUI) == 31

    def test_anniversaire_a_venir_ne_compte_pas(self):
        assert age(date(1995, 12, 31), aujourdhui=AUJOURDHUI) == 30

    def test_anniversaire_le_jour_meme(self):
        assert age(date(1995, 9, 15), aujourdhui=AUJOURDHUI) == 31


class TestMifflinStJeor:
    def test_homme(self):
        """10×70 + 6,25×175 − 5×31 + 5 = 1643,75"""
        assert bmr(Decimal("70"), Decimal("175"), 31, Sex.MALE) == Decimal("1643.75")

    def test_femme(self):
        """10×60 + 6,25×165 − 5×30 − 161 = 1320,25"""
        assert bmr(Decimal("60"), Decimal("165"), 30, Sex.FEMALE) == Decimal("1320.25")

    def test_non_specifie_applique_la_formule_feminine(self):
        """En cas de doute, on ne surestime pas les besoins d'une personne."""
        femme = bmr(Decimal("70"), Decimal("175"), 31, Sex.FEMALE)
        inconnu = bmr(Decimal("70"), Decimal("175"), 31, Sex.UNSPECIFIED)

        assert inconnu == femme

    @pytest.mark.parametrize("niveau", list(ActivityLevel))
    def test_tous_les_niveaux_ont_un_facteur(self, niveau):
        assert tdee(Decimal("1600"), niveau) == Decimal("1600") * FACTEUR_ACTIVITE[niveau]

    def test_poids_ou_taille_nuls_refuses(self):
        with pytest.raises(ValueError):
            besoins(poids_kg=Decimal("0"))
        with pytest.raises(ValueError):
            besoins(taille_cm=Decimal("0"))


class TestCibleParObjectif:
    def test_maintien_egale_le_tdee(self):
        b = besoins(objectif=Goal.WEIGHT_MAINTENANCE)
        assert b.kcal_target == b.tdee

    def test_perte_de_poids_retire_20_pourcent(self):
        b = besoins(objectif=Goal.WEIGHT_LOSS)
        assert b.kcal_target < b.tdee
        assert b.safety_floor_applied is False

    def test_prise_de_poids_ajoute_15_pourcent(self):
        b = besoins(objectif=Goal.WEIGHT_GAIN)
        assert b.kcal_target > b.tdee

    def test_critere_d_acceptation_de_la_spec(self):
        """« Un programme prise de masse apporte mesurablement plus de calories
        qu'un programme perte de poids pour un même profil » — test exigé."""
        masse = besoins(objectif=Goal.MUSCLE_GAIN)
        perte = besoins(objectif=Goal.WEIGHT_LOSS)

        assert masse.kcal_target > perte.kcal_target


class TestPlancherDeSecurite:
    """🔴 Le contrôle qui engage le plus. Un régime sous le plancher proposé par
    un logiciel est un problème de santé."""

    def test_jamais_sous_le_bmr(self):
        """Profil sédentaire en perte de poids : TDEE − 20 % passe sous le BMR."""
        b = besoins(
            objectif=Goal.WEIGHT_LOSS, niveau_activite=ActivityLevel.SEDENTARY
        )

        assert b.kcal_target >= b.bmr
        assert b.safety_floor_applied is True

    def test_jamais_sous_le_plancher_absolu_femme(self):
        b = besoins(
            poids_kg=Decimal("45"),
            taille_cm=Decimal("150"),
            sexe=Sex.FEMALE,
            objectif=Goal.WEIGHT_LOSS,
            niveau_activite=ActivityLevel.SEDENTARY,
        )

        assert b.kcal_target >= PLANCHER_ABSOLU[Sex.FEMALE]

    def test_jamais_sous_le_plancher_absolu_homme(self):
        b = besoins(
            poids_kg=Decimal("50"),
            taille_cm=Decimal("160"),
            sexe=Sex.MALE,
            objectif=Goal.WEIGHT_LOSS,
            niveau_activite=ActivityLevel.SEDENTARY,
        )

        assert b.kcal_target >= PLANCHER_ABSOLU[Sex.MALE]

    def test_le_plancher_est_signale(self):
        """Relever la cible en silence priverait l'utilisateur de l'information
        que la spec lui doit."""
        b = besoins(
            poids_kg=Decimal("45"),
            taille_cm=Decimal("150"),
            sexe=Sex.FEMALE,
            objectif=Goal.WEIGHT_LOSS,
            niveau_activite=ActivityLevel.SEDENTARY,
        )

        assert b.safety_floor_applied is True

    def test_aucun_profil_courant_ne_descend_sous_le_plancher(self):
        """Balayage : aucune combinaison ne doit produire une cible dangereuse."""
        for sexe in Sex:
            for niveau in ActivityLevel:
                for objectif in Goal:
                    for poids in (Decimal("42"), Decimal("70"), Decimal("110")):
                        b = besoins(
                            poids_kg=poids,
                            sexe=sexe,
                            niveau_activite=niveau,
                            objectif=objectif,
                        )
                        assert b.kcal_target >= PLANCHER_ABSOLU[sexe]
                        assert b.kcal_target >= b.bmr


class TestMacronutriments:
    def test_les_proteines_suivent_le_poids(self):
        leger = besoins(poids_kg=Decimal("50"))
        lourd = besoins(poids_kg=Decimal("90"))

        assert lourd.protein_g > leger.protein_g

    def test_prise_de_masse_plus_proteinee_que_maintien(self):
        masse = besoins(objectif=Goal.MUSCLE_GAIN)
        maintien = besoins(objectif=Goal.WEIGHT_MAINTENANCE)

        assert masse.protein_g > maintien.protein_g

    def test_plancher_lipidique_respecte(self):
        for objectif in Goal:
            for poids in (Decimal("45"), Decimal("70"), Decimal("110")):
                b = besoins(objectif=objectif, poids_kg=poids)
                assert b.fat_g >= PLANCHER_LIPIDES_G_PAR_KG * poids

    def test_les_glucides_ne_sont_jamais_negatifs(self):
        """Un macro négatif se propagerait en silence dans le scoring."""
        b = besoins(
            poids_kg=Decimal("110"),
            taille_cm=Decimal("150"),
            objectif=Goal.MUSCLE_GAIN,
            niveau_activite=ActivityLevel.SEDENTARY,
        )

        assert b.carbs_g >= 0

    def test_la_formule_et_sa_version_sont_enregistrees(self):
        """Sans elles, l'historique cesse d'être interprétable après un
        changement de formule."""
        b = besoins()

        assert b.formula == "mifflin_st_jeor"
        assert b.formula_version
