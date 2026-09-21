"""Couverture du catalogue — sprint 03, §2 et §5.

Le sprint 03 le dit sans détour : *« Vérifier qu'aucune combinaison fréquente ne
se retrouve à zéro résultat (c'est le test qui compte, plus que le nombre
total). »* Compter 180 plats ne prouve rien si tous sont omnivores : un
utilisateur végétarien en prise de masse n'obtiendrait aucun résultat.

Ce module calcule donc deux choses :

* l'**avancement** vers les cibles du lot 1 (~150 ingrédients tous signés,
  ~180 plats publiés) ;
* la **couverture réelle**, croisement objectif × restriction × type de repas,
  confrontée au pool minimal exigé par la spécification (§6.6).

Aucune base n'est nécessaire : les fonctions prennent des enregistrements déjà
lus. C'est ce qui les rend testables à chaque commit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.models.enums import DishStatus, Goal, MealType, RestrictionType

#: Cibles du lot 1 (FN-040, et seeds/README.md « Volumétrie cible »).
CIBLE_INGREDIENTS = 150
CIBLE_PLATS_PUBLIES = 180

#: Répartition attendue des plats publiés par type de repas (seeds/README.md).
CIBLE_PAR_TYPE_REPAS: dict[MealType, int] = {
    MealType.BREAKFAST: 30,
    MealType.LUNCH: 75,
    MealType.DINNER: 75,
}

#: §6.6 — les filtres bloquants éliminent 40 à 70 % du catalogue ; le pool
#: survivant doit valoir 3 à 5× le nombre de créneaux pour que le générateur ne
#: soit jamais bloqué. Pour 7 jours : 7 créneaux → 25 minimum.
POOL_MINIMAL_PAR_CRENEAU = 25

#: Les restrictions qu'un utilisateur déclare le plus souvent. `RELIGIOUS`,
#: `FORBIDDEN_FOOD` et `CUSTOM` en sont exclues : elles ne se réduisent pas à un
#: drapeau de plat et se filtrent au cas par cas.
RESTRICTIONS_COURANTES: tuple[RestrictionType, ...] = (
    RestrictionType.VEGETARIAN,
    RestrictionType.VEGAN,
    RestrictionType.NO_PORK,
    RestrictionType.LACTOSE_FREE,
    RestrictionType.GLUTEN_FREE,
)


@dataclass(frozen=True)
class PlatResume:
    """Un plat réduit à ce dont la couverture a besoin."""

    slug: str
    status: DishStatus
    meal_types: tuple[str, ...] = ()
    compatible_goals: tuple[str, ...] = ()
    compatible_restrictions: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngredientResume:
    slug: str
    allergenes_verifies: bool


@dataclass(frozen=True)
class Creneau:
    """Une combinaison objectif × restriction × type de repas, et son compte."""

    objectif: Goal
    restriction: RestrictionType | None
    type_repas: MealType
    plats: int

    @property
    def vide(self) -> bool:
        return self.plats == 0

    @property
    def insuffisant(self) -> bool:
        return self.plats < POOL_MINIMAL_PAR_CRENEAU


@dataclass
class RapportCouverture:
    ingredients_total: int = 0
    ingredients_verifies: int = 0
    plats_par_statut: Counter[str] = field(default_factory=Counter)
    plats_par_type_repas: Counter[str] = field(default_factory=Counter)
    creneaux: list[Creneau] = field(default_factory=list)

    @property
    def plats_publies(self) -> int:
        return self.plats_par_statut.get(DishStatus.PUBLISHED.value, 0)

    @property
    def creneaux_vides(self) -> list[Creneau]:
        return [c for c in self.creneaux if c.vide]

    @property
    def creneaux_insuffisants(self) -> list[Creneau]:
        return [c for c in self.creneaux if c.insuffisant and not c.vide]

    @property
    def pret_pour_le_lot_2(self) -> bool:
        """Le seuil qui débloque le générateur : la cible de plats est atteinte,
        tous les ingrédients sont signés, et aucune combinaison n'est à zéro."""
        return (
            self.plats_publies >= CIBLE_PLATS_PUBLIES
            and self.ingredients_total >= CIBLE_INGREDIENTS
            and self.ingredients_verifies == self.ingredients_total
            and not self.creneaux_vides
        )


def _correspond(plat: PlatResume, restriction: RestrictionType | None) -> bool:
    if restriction is None:
        return True
    return restriction.value in plat.compatible_restrictions


def analyser(
    plats: list[PlatResume],
    ingredients: list[IngredientResume],
    *,
    restrictions: tuple[RestrictionType, ...] = RESTRICTIONS_COURANTES,
) -> RapportCouverture:
    """Croise le catalogue publié et renvoie l'état de couverture.

    Seuls les plats `published` comptent dans les créneaux : un plat en
    `draft` n'est pas recommandable, et le faire figurer donnerait une image
    flatteuse et fausse de l'avancement.
    """
    rapport = RapportCouverture(
        ingredients_total=len(ingredients),
        ingredients_verifies=sum(1 for i in ingredients if i.allergenes_verifies),
    )

    for plat in plats:
        rapport.plats_par_statut[str(plat.status)] += 1

    publies = [p for p in plats if p.status == DishStatus.PUBLISHED]
    for plat in publies:
        for type_repas in plat.meal_types:
            rapport.plats_par_type_repas[type_repas] += 1

    # `None` en tête : la couverture sans restriction est la référence dont on
    # mesure l'érosion restriction par restriction.
    for objectif in Goal:
        for restriction in (None, *restrictions):
            for type_repas in MealType:
                compte = sum(
                    1
                    for p in publies
                    if objectif.value in p.compatible_goals
                    and type_repas.value in p.meal_types
                    and _correspond(p, restriction)
                )
                rapport.creneaux.append(
                    Creneau(
                        objectif=objectif,
                        restriction=restriction,
                        type_repas=type_repas,
                        plats=compte,
                    )
                )

    return rapport
