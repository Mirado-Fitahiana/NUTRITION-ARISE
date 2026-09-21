"""Besoins énergétiques et macronutriments — FN-038.

*« C'est le cœur nutritionnel du module ; il était absent de la version 1.0. »*
Rien ne le calculait jusqu'ici : la table `nutrition_targets` existait, le
modèle aussi, mais aucun service ne la remplissait — et le moteur de
recommandation y lit sa cible calorique.

Fonctions **pures**, sans base, comme `nutrition.py` et `units.py`.

Le point qui engage le plus : **le plancher de sécurité est impératif**. Une
cible calculée sous le plancher est relevée, et `safety_floor_applied` le dit —
un régime à 900 kcal proposé par un logiciel, c'est un problème de santé, pas un
défaut d'arrondi.

Ces valeurs sont des **estimations statistiques, pas une prescription
médicale** (FN-039).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.models.enums import ActivityLevel, Goal, Sex

FORMULE = "mifflin_st_jeor"
FORMULE_VERSION = "v1"

#: FN-038 — facteurs d'activité.
FACTEUR_ACTIVITE: dict[ActivityLevel, Decimal] = {
    ActivityLevel.SEDENTARY: Decimal("1.20"),
    ActivityLevel.LIGHTLY_ACTIVE: Decimal("1.375"),
    ActivityLevel.MODERATELY_ACTIVE: Decimal("1.55"),
    ActivityLevel.VERY_ACTIVE: Decimal("1.725"),
    ActivityLevel.EXTREMELY_ACTIVE: Decimal("1.90"),
}

#: Ajustement de la cible calorique, en proportion du TDEE.
AJUSTEMENT_OBJECTIF: dict[Goal, Decimal] = {
    Goal.WEIGHT_LOSS: Decimal("-0.20"),
    Goal.WEIGHT_MAINTENANCE: Decimal("0"),
    Goal.WEIGHT_GAIN: Decimal("0.15"),
    Goal.MUSCLE_GAIN: Decimal("0.12"),
    Goal.BALANCED_DIET: Decimal("0"),
    Goal.HABIT_IMPROVEMENT: Decimal("0"),
}

#: 🔴 Plancher absolu, par sexe. `UNSPECIFIED` prend la valeur féminine, la
#: plus basse — on ne relève jamais un plancher au-dessus de ce que le profil
#: permet d'affirmer.
PLANCHER_ABSOLU: dict[Sex, Decimal] = {
    Sex.MALE: Decimal("1500"),
    Sex.FEMALE: Decimal("1200"),
    Sex.UNSPECIFIED: Decimal("1200"),
}

#: Protéines en g/kg de poids corporel.
PROTEINES_G_PAR_KG: dict[Goal, Decimal] = {
    Goal.WEIGHT_LOSS: Decimal("1.8"),
    Goal.WEIGHT_MAINTENANCE: Decimal("1.2"),
    Goal.BALANCED_DIET: Decimal("1.2"),
    Goal.HABIT_IMPROVEMENT: Decimal("1.2"),
    Goal.WEIGHT_GAIN: Decimal("1.6"),
    Goal.MUSCLE_GAIN: Decimal("2.0"),
}

#: Part des calories apportée par les lipides.
PART_LIPIDES: dict[Goal, Decimal] = {
    Goal.WEIGHT_LOSS: Decimal("0.25"),
    Goal.WEIGHT_MAINTENANCE: Decimal("0.30"),
    Goal.BALANCED_DIET: Decimal("0.30"),
    Goal.HABIT_IMPROVEMENT: Decimal("0.30"),
    Goal.WEIGHT_GAIN: Decimal("0.30"),
    Goal.MUSCLE_GAIN: Decimal("0.25"),
}

#: Plancher lipidique absolu, en g/kg.
PLANCHER_LIPIDES_G_PAR_KG = Decimal("0.8")

KCAL_PAR_G_PROTEINE = Decimal("4")
KCAL_PAR_G_GLUCIDE = Decimal("4")
KCAL_PAR_G_LIPIDE = Decimal("9")


@dataclass(frozen=True)
class Besoins:
    """FN-038 — ce qui sera stocké dans `nutrition_targets`."""

    bmr: Decimal
    tdee: Decimal
    kcal_target: Decimal
    protein_g: Decimal
    carbs_g: Decimal
    fat_g: Decimal
    #: 🔴 Vrai quand la cible a été **relevée** au plancher. L'utilisateur doit
    #: en être informé (FN-038).
    safety_floor_applied: bool
    formula: str = FORMULE
    formula_version: str = FORMULE_VERSION


def _arrondi(valeur: Decimal) -> Decimal:
    return valeur.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def age(birth_date: date, *, aujourdhui: date | None = None) -> int:
    """Âge révolu. Un anniversaire non encore passé cette année ne compte pas."""
    reference = aujourdhui or date.today()
    ans = reference.year - birth_date.year
    if (reference.month, reference.day) < (birth_date.month, birth_date.day):
        ans -= 1
    return max(0, ans)


def bmr(
    poids_kg: Decimal, taille_cm: Decimal, ans: int, sexe: Sex
) -> Decimal:
    """Mifflin-St Jeor.

    `UNSPECIFIED` applique la formule féminine, minorante : en cas de doute, on
    ne surestime pas les besoins d'une personne.
    """
    base = (
        Decimal("10") * poids_kg
        + Decimal("6.25") * taille_cm
        - Decimal("5") * Decimal(ans)
    )
    correction = Decimal("5") if sexe == Sex.MALE else Decimal("-161")
    return _arrondi(base + correction)


def tdee(valeur_bmr: Decimal, niveau: ActivityLevel) -> Decimal:
    return _arrondi(valeur_bmr * FACTEUR_ACTIVITE[niveau])


def calculer(
    *,
    poids_kg: Decimal,
    taille_cm: Decimal,
    birth_date: date,
    sexe: Sex,
    niveau_activite: ActivityLevel,
    objectif: Goal,
    aujourdhui: date | None = None,
) -> Besoins:
    """Chaîne complète : BMR → TDEE → cible → macros, planchers compris."""
    if poids_kg <= 0 or taille_cm <= 0:
        raise ValueError("poids et taille doivent être strictement positifs")

    ans = age(birth_date, aujourdhui=aujourdhui)
    valeur_bmr = bmr(poids_kg, taille_cm, ans, sexe)
    valeur_tdee = tdee(valeur_bmr, niveau_activite)

    cible_brute = valeur_tdee * (Decimal("1") + AJUSTEMENT_OBJECTIF[objectif])

    # 🔴 Deux planchers, tous deux impératifs : jamais sous le métabolisme de
    # base, jamais sous le minimum absolu. On prend le plus haut des deux.
    plancher = max(valeur_bmr, PLANCHER_ABSOLU[sexe])
    plancher_applique = cible_brute < plancher
    cible = plancher if plancher_applique else cible_brute

    proteines = PROTEINES_G_PAR_KG[objectif] * poids_kg

    lipides = (PART_LIPIDES[objectif] * cible) / KCAL_PAR_G_LIPIDE
    lipides = max(lipides, PLANCHER_LIPIDES_G_PAR_KG * poids_kg)

    kcal_restantes = (
        cible - proteines * KCAL_PAR_G_PROTEINE - lipides * KCAL_PAR_G_LIPIDE
    )
    # Sur un profil léger avec un objectif très protéiné, protéines et lipides
    # peuvent à eux seuls dépasser la cible. Les glucides tombent alors à zéro
    # plutôt que de devenir négatifs — un macro négatif se propagerait en
    # silence dans le scoring.
    glucides = max(Decimal("0"), kcal_restantes / KCAL_PAR_G_GLUCIDE)

    return Besoins(
        bmr=valeur_bmr,
        tdee=valeur_tdee,
        kcal_target=_arrondi(cible),
        protein_g=_arrondi(proteines),
        carbs_g=_arrondi(glucides),
        fat_g=_arrondi(lipides),
        safety_floor_applied=plancher_applique,
    )


__all__ = [
    "AJUSTEMENT_OBJECTIF",
    "Besoins",
    "FACTEUR_ACTIVITE",
    "FORMULE",
    "FORMULE_VERSION",
    "PLANCHER_ABSOLU",
    "PLANCHER_LIPIDES_G_PAR_KG",
    "age",
    "bmr",
    "calculer",
    "tdee",
]
