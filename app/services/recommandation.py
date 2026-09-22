"""Moteur de recommandation — Lot 2 (FN-019 → FN-024, D-06, D-07).

**D-06 : la sélection est 100 % déterministe.** Filtrage SQL, score métier,
composition, validation — aucun modèle de langage n'intervient. Le LLM ne
rédige que la justification d'un programme *déjà entièrement décidé*
(FN-021), en **un seul appel** (D-07). Le motif est inscrit dans la spec :
*« hallucinations sur des données de santé »*.

Ce module ne connaît ni SQLAlchemy ni HTTP. Il prend des plats déjà lus et
rend un programme. C'est ce qui le rend testable en millisecondes, et ce qui
permet de vérifier à chaque commit qu'un allergène ne peut pas passer.

Trois invariants portent la sécurité alimentaire :

1. **Le filtrage dur précède toujours le score.** Un allergène n'est jamais
   écarté par un score trop bas : il est écarté parce qu'il est interdit.
2. **Un vivier insuffisant ne produit pas un programme dégradé.** Il produit un
   refus explicite. Avec un catalogue jeune, c'est le cas le plus probable.
3. **La validation finale rejoue les contrôles**, y compris après le LLM. Le
   texte arrive après la sélection ; rien ne garantit qu'il ne l'ait pas
   déformée.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from app.models.enums import Allergen, Goal, MealSlot, RestrictionType

#: FN-023 — répartition des calories par créneau. Valeur de repli : la vraie
#: vit dans `app_settings.meal_distribution`.
REPARTITION_PAR_DEFAUT: dict[MealSlot, Decimal] = {
    MealSlot.BREAKFAST: Decimal("0.25"),
    MealSlot.MORNING_SNACK: Decimal("0.05"),
    MealSlot.LUNCH: Decimal("0.35"),
    MealSlot.AFTERNOON_SNACK: Decimal("0.05"),
    MealSlot.DINNER: Decimal("0.30"),
}

#: Créneaux qu'un plat de ce type peut occuper.
SLOTS_PAR_TYPE: dict[str, tuple[MealSlot, ...]] = {
    "breakfast": (MealSlot.BREAKFAST,),
    "lunch": (MealSlot.LUNCH,),
    "dinner": (MealSlot.DINNER,),
    "snack": (MealSlot.MORNING_SNACK, MealSlot.AFTERNOON_SNACK),
}

#: FN-020 — poids de repli. Les vrais viennent de `scoring_weight_sets`, dont
#: la version est tracée dans chaque `recommendation_run`.
POIDS_PAR_DEFAUT: dict[str, Decimal] = {
    "nutrition": Decimal("0.45"),
    "cost": Decimal("0.20"),
    "preference": Decimal("0.15"),
    "variety": Decimal("0.15"),
    "favorite": Decimal("0.05"),
}


@dataclass(frozen=True)
class PlatCandidat:
    """Un plat publié, réduit à ce dont le moteur a besoin."""

    slug: str
    name: str
    meal_types: frozenset[str]
    kcal: Decimal
    protein_g: Decimal = Decimal("0")
    carbs_g: Decimal = Decimal("0")
    fat_g: Decimal = Decimal("0")
    allergens: frozenset[Allergen] = frozenset()
    compatible_restrictions: frozenset[RestrictionType] = frozenset()
    compatible_goals: frozenset[Goal] = frozenset()
    ingredient_slugs: frozenset[str] = frozenset()
    #: Sources de protéines, pour la rotation (FN-024).
    protein_sources: frozenset[str] = frozenset()
    estimated_cost: Decimal | None = None


@dataclass(frozen=True)
class ContraintesUtilisateur:
    """Ce que l'utilisateur impose — et ce qu'il préfère seulement.

    La distinction est structurante : `allergens`, `restrictions` et
    `disliked_ingredients` **excluent** ; `preferred_ingredients` et
    `favorite_ingredients` ne font que moduler le score.
    """

    goal: Goal
    kcal_target: Decimal
    allergens: frozenset[Allergen] = frozenset()
    restrictions: frozenset[RestrictionType] = frozenset()
    disliked_ingredients: frozenset[str] = frozenset()
    preferred_ingredients: frozenset[str] = frozenset()
    favorite_ingredients: frozenset[str] = frozenset()
    budget_par_jour: Decimal | None = None


@dataclass(frozen=True)
class ReglesComposition:
    """Lues depuis `app_settings` — jamais codées en dur."""

    max_plan_days: int = 7
    min_pool_size: int = 25
    max_repeats: int = 2
    min_gap_days: int = 2
    protein_rotation_window: int = 3
    daily_kcal_tolerance: Decimal = Decimal("0.10")
    meal_kcal_tolerance: Decimal = Decimal("0.20")
    meal_distribution: dict[MealSlot, Decimal] = field(
        default_factory=lambda: dict(REPARTITION_PAR_DEFAUT)
    )


@dataclass(frozen=True)
class MotifExclusion:
    """Pourquoi un plat a été écarté. Conservé : sans lui, un vivier vide est
    indébuggable."""

    slug: str
    motif: str


@dataclass(frozen=True)
class Vivier:
    retenus: tuple[PlatCandidat, ...]
    exclus: tuple[MotifExclusion, ...]

    @property
    def taille(self) -> int:
        return len(self.retenus)


@dataclass(frozen=True)
class ScoreDetaille:
    """FN-020 — le score et sa décomposition, pour pouvoir l'expliquer."""

    total: Decimal
    detail: dict[str, Decimal]


@dataclass(frozen=True)
class RepasPropose:
    slot: MealSlot
    plat: PlatCandidat
    kcal_cible: Decimal
    score: ScoreDetaille


@dataclass(frozen=True)
class JourneeProposee:
    index: int
    repas: tuple[RepasPropose, ...]

    @property
    def kcal_total(self) -> Decimal:
        return sum((r.plat.kcal for r in self.repas), Decimal("0"))


@dataclass(frozen=True)
class EchecGeneration:
    """FN-037 — un refus explicite, avec un code stable."""

    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceCreneau:
    """Ce que `composer` a évalué pour un créneau — pour le laboratoire.

    Sans cette trace, on ne voit que le plat retenu ; on ne sait ni quels plats
    il a battus, ni lesquels les règles de variété avaient déjà écartés.
    """

    jour: int
    slot: MealSlot
    kcal_cible: Decimal
    #: Candidats notés, du meilleur au moins bon.
    candidats: tuple[tuple[ScoreDetaille, PlatCandidat], ...]
    #: Plats éligibles écartés par une règle de variété, avec le motif.
    ecartes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProgrammeGenere:
    journees: tuple[JourneeProposee, ...]
    seed: int
    vivier_taille: int
    #: Créneaux qu'aucun plat n'a pu couvrir, jour par jour.
    creneaux_non_couverts: tuple[tuple[int, MealSlot], ...] = ()


# --------------------------------------------------------------------------
# FN-019 — filtrage dur
# --------------------------------------------------------------------------


def filtrer(
    plats: list[PlatCandidat], contraintes: ContraintesUtilisateur
) -> Vivier:
    """FN-019 — écarte ce qui est **interdit**, avant tout calcul de score.

    L'ordre n'est pas une commodité d'implémentation : un allergène écarté par
    un score trop bas serait un allergène qu'un ajustement de poids peut
    réintroduire. Ici, il est écarté parce qu'il est interdit.
    """
    retenus: list[PlatCandidat] = []
    exclus: list[MotifExclusion] = []

    for plat in plats:
        communs = plat.allergens & contraintes.allergens
        if communs:
            exclus.append(
                MotifExclusion(plat.slug, f"allergène : {', '.join(sorted(communs))}")
            )
            continue

        manquantes = contraintes.restrictions - plat.compatible_restrictions
        if manquantes:
            exclus.append(
                MotifExclusion(
                    plat.slug, f"restriction non satisfaite : {', '.join(sorted(manquantes))}"
                )
            )
            continue

        refuses = plat.ingredient_slugs & contraintes.disliked_ingredients
        if refuses:
            exclus.append(
                MotifExclusion(plat.slug, f"ingrédient refusé : {', '.join(sorted(refuses))}")
            )
            continue

        retenus.append(plat)

    return Vivier(retenus=tuple(retenus), exclus=tuple(exclus))


# --------------------------------------------------------------------------
# FN-020 — score métier
# --------------------------------------------------------------------------


def _adequation_kcal(kcal: Decimal, cible: Decimal) -> Decimal:
    """1 quand le plat tombe sur la cible, décroît linéairement ensuite."""
    if cible <= 0:
        return Decimal("0")
    ecart = abs(kcal - cible) / cible
    return max(Decimal("0"), Decimal("1") - ecart)


def scorer(
    plat: PlatCandidat,
    slot: MealSlot,
    kcal_cible: Decimal,
    contraintes: ContraintesUtilisateur,
    *,
    poids: dict[str, Decimal] | None = None,
    deja_utilise: int = 0,
    budget_repas: Decimal | None = None,
) -> ScoreDetaille:
    """FN-020 — combine adéquation nutritionnelle, coût, préférences, variété.

    Les préférences **modulent**, elles n'excluent pas : un plat non préféré
    reste sélectionnable, il passe simplement après.
    """
    p = {**POIDS_PAR_DEFAUT, **(poids or {})}

    nutrition = _adequation_kcal(plat.kcal, kcal_cible)

    if budget_repas is None or plat.estimated_cost is None:
        cout = Decimal("0.5")  # inconnu : ni favorisé ni pénalisé
    elif budget_repas <= 0:
        cout = Decimal("0")
    else:
        cout = max(
            Decimal("0"), Decimal("1") - (plat.estimated_cost / budget_repas)
        )
        cout = min(Decimal("1"), cout)

    preference = (
        Decimal("1")
        if plat.ingredient_slugs & contraintes.preferred_ingredients
        else Decimal("0.5")
    )
    favori = (
        Decimal("1")
        if plat.ingredient_slugs & contraintes.favorite_ingredients
        else Decimal("0")
    )
    # FN-024 — chaque réutilisation dégrade le score, sans jamais interdire :
    # l'interdiction relève de `max_repeats`, pas du score.
    variete = Decimal("1") / Decimal(1 + deja_utilise)

    if contraintes.goal in plat.compatible_goals:
        bonus_objectif = Decimal("1")
    elif plat.compatible_goals:
        bonus_objectif = Decimal("0.4")
    else:
        bonus_objectif = Decimal("0.7")

    detail = {
        "nutrition": (nutrition * p["nutrition"]).quantize(Decimal("0.000001")),
        "cost": (cout * p["cost"]).quantize(Decimal("0.000001")),
        "preference": (preference * p["preference"]).quantize(Decimal("0.000001")),
        "variety": (variete * p["variety"]).quantize(Decimal("0.000001")),
        "favorite": (favori * p["favorite"]).quantize(Decimal("0.000001")),
    }
    total = (sum(detail.values(), Decimal("0")) * bonus_objectif).quantize(
        Decimal("0.000001")
    )
    detail["goal_multiplier"] = bonus_objectif
    return ScoreDetaille(total=total, detail=detail)


# --------------------------------------------------------------------------
# FN-023 / FN-024 — composition
# --------------------------------------------------------------------------


def _eligibles(vivier: list[PlatCandidat], slot: MealSlot) -> list[PlatCandidat]:
    return [
        plat
        for plat in vivier
        if any(slot in SLOTS_PAR_TYPE.get(t, ()) for t in plat.meal_types)
    ]


def composer(
    vivier: Vivier,
    contraintes: ContraintesUtilisateur,
    jours: int,
    regles: ReglesComposition,
    *,
    poids: dict[str, Decimal] | None = None,
    seed: int | None = None,
    trace: list[TraceCreneau] | None = None,
) -> ProgrammeGenere | EchecGeneration:
    """FN-023 / FN-024 — construit le programme, ou explique pourquoi il ne peut pas.

    Le `seed` rend la génération **rejouable à l'identique** : sans lui, une
    régression de qualité est indébuggable.

    `trace`, quand elle est fournie, reçoit le détail de chaque créneau. Elle
    n'influe sur aucune décision : le laboratoire l'utilise pour montrer le
    classement, la génération réelle ne la passe pas.
    """
    if jours < 1:
        return EchecGeneration("PERIOD_INVALID", "La durée doit valoir au moins un jour.")
    if jours > regles.max_plan_days:
        return EchecGeneration(
            "PERIOD_INVALID",
            f"La durée maximale est de {regles.max_plan_days} jours.",
            {"requested": jours, "max": regles.max_plan_days},
        )

    if not vivier.retenus:
        return EchecGeneration(
            "NO_COMPATIBLE_DISH",
            "Aucun plat du catalogue ne satisfait vos contraintes.",
            {"exclus": len(vivier.exclus)},
        )

    if vivier.taille < regles.min_pool_size:
        # Le cas le plus probable avec un catalogue jeune. Un programme bâti
        # sur un vivier trop maigre serait répétitif : mieux vaut le dire.
        return EchecGeneration(
            "CATALOG_TOO_SMALL",
            (
                f"Le catalogue ne propose que {vivier.taille} plat(s) compatible(s), "
                f"il en faut au moins {regles.min_pool_size} pour composer un "
                "programme varié."
            ),
            {"pool": vivier.taille, "required": regles.min_pool_size},
        )

    graine = seed if seed is not None else random.randrange(2**31 - 1)
    alea = random.Random(graine)

    disponibles = list(vivier.retenus)
    # Le tri par slug avant mélange rend l'ordre reproductible quel que soit
    # l'ordre de lecture en base.
    disponibles.sort(key=lambda p: p.slug)
    alea.shuffle(disponibles)

    utilisations: dict[str, int] = {}
    dernier_jour: dict[str, int] = {}
    proteines_recentes: list[frozenset[str]] = []

    journees: list[JourneeProposee] = []
    non_couverts: list[tuple[int, MealSlot]] = []

    for index_jour in range(jours):
        repas: list[RepasPropose] = []
        proteines_du_jour: set[str] = set()

        for slot, part in regles.meal_distribution.items():
            kcal_cible = (contraintes.kcal_target * part).quantize(Decimal("0.01"))
            budget_repas = (
                (contraintes.budget_par_jour * part)
                if contraintes.budget_par_jour is not None
                else None
            )

            candidats = []
            ecartes: list[tuple[str, str]] = []
            for plat in _eligibles(disponibles, slot):
                if utilisations.get(plat.slug, 0) >= regles.max_repeats:
                    if trace is not None:
                        ecartes.append(
                            (plat.slug, f"déjà servi {regles.max_repeats} fois (max_repeats)")
                        )
                    continue
                precedent = dernier_jour.get(plat.slug)
                if precedent is not None and index_jour - precedent < regles.min_gap_days:
                    if trace is not None:
                        ecartes.append(
                            (plat.slug, f"servi il y a moins de {regles.min_gap_days} jours (min_gap_days)")
                        )
                    continue
                if any(p.plat.slug == plat.slug for p in repas):
                    if trace is not None:
                        ecartes.append((plat.slug, "déjà au menu de ce jour"))
                    continue

                score = scorer(
                    plat,
                    slot,
                    kcal_cible,
                    contraintes,
                    poids=poids,
                    deja_utilise=utilisations.get(plat.slug, 0),
                    budget_repas=budget_repas,
                )

                # FN-024 — la rotation des protéines dégrade, elle n'interdit pas :
                # sur un catalogue étroit, interdire viderait le créneau.
                penalite = Decimal("1")
                fenetre = proteines_recentes[-regles.protein_rotation_window :]
                if plat.protein_sources and any(
                    plat.protein_sources & recentes for recentes in fenetre
                ):
                    penalite = Decimal("0.7")
                if plat.protein_sources & proteines_du_jour:
                    penalite = Decimal("0.5")

                candidats.append(
                    (
                        ScoreDetaille(
                            total=(score.total * penalite).quantize(Decimal("0.000001")),
                            detail={**score.detail, "protein_rotation": penalite},
                        ),
                        plat,
                    )
                )

            # Tri par score puis slug : à score égal, le choix reste déterministe.
            candidats.sort(key=lambda c: (-c[0].total, c[1].slug))
            if trace is not None:
                trace.append(
                    TraceCreneau(
                        jour=index_jour,
                        slot=slot,
                        kcal_cible=kcal_cible,
                        candidats=tuple(candidats),
                        ecartes=tuple(ecartes),
                    )
                )

            if not candidats:
                non_couverts.append((index_jour, slot))
                continue

            meilleur_score, meilleur = candidats[0]

            repas.append(
                RepasPropose(
                    slot=slot, plat=meilleur, kcal_cible=kcal_cible, score=meilleur_score
                )
            )
            utilisations[meilleur.slug] = utilisations.get(meilleur.slug, 0) + 1
            dernier_jour[meilleur.slug] = index_jour
            proteines_du_jour |= meilleur.protein_sources

        proteines_recentes.append(frozenset(proteines_du_jour))
        journees.append(JourneeProposee(index=index_jour, repas=tuple(repas)))

    if all(not j.repas for j in journees):
        return EchecGeneration(
            "NO_COMPATIBLE_DISH",
            "Aucun créneau n'a pu être couvert avec les plats disponibles.",
            {"pool": vivier.taille},
        )

    return ProgrammeGenere(
        journees=tuple(journees),
        seed=graine,
        vivier_taille=vivier.taille,
        creneaux_non_couverts=tuple(non_couverts),
    )


# --------------------------------------------------------------------------
# FN-022 — validation finale
# --------------------------------------------------------------------------

#: Contrôles 1 à 4 de FN-022 : leur échec est un **incident de sécurité
#: alimentaire**, pas un défaut de qualité.
CONTROLES_SECURITE = frozenset({1, 2, 3, 4})


@dataclass(frozen=True)
class ResultatValidation:
    conforme: bool
    controles_en_echec: tuple[int, ...] = ()
    incident_securite: bool = False
    details: dict[str, object] = field(default_factory=dict)


def valider(
    programme: ProgrammeGenere,
    contraintes: ContraintesUtilisateur,
    regles: ReglesComposition,
    *,
    catalogue_autorise: frozenset[str] | None = None,
) -> ResultatValidation:
    """FN-022 — rejoue les contrôles sur le programme **final**.

    Exécutée deux fois : après la sélection, puis après le LLM. La seconde
    passe n'est pas redondante — le texte arrive après la sélection, et rien ne
    garantit qu'il ne l'ait pas déformée.

    | Contrôle | Objet |
    |---|---|
    | 1 | aucun allergène déclaré |
    | 2 | restrictions respectées |
    | 3 | aucun ingrédient refusé |
    | 4 | tout plat provient du catalogue |
    | 5 | calories du jour dans la tolérance |
    """
    echecs: set[int] = set()
    details: dict[str, object] = {}

    allergenes_trouves: set[str] = set()
    hors_catalogue: set[str] = set()

    for journee in programme.journees:
        for repas in journee.repas:
            plat = repas.plat
            if plat.allergens & contraintes.allergens:
                echecs.add(1)
                allergenes_trouves |= {
                    str(a) for a in plat.allergens & contraintes.allergens
                }
            if contraintes.restrictions - plat.compatible_restrictions:
                echecs.add(2)
            if plat.ingredient_slugs & contraintes.disliked_ingredients:
                echecs.add(3)
            if catalogue_autorise is not None and plat.slug not in catalogue_autorise:
                echecs.add(4)
                hors_catalogue.add(plat.slug)

        if journee.repas and contraintes.kcal_target > 0:
            ecart = abs(journee.kcal_total - contraintes.kcal_target) / contraintes.kcal_target
            if ecart > regles.daily_kcal_tolerance:
                echecs.add(5)
                details.setdefault("kcal_ecarts", []).append(  # type: ignore[union-attr]
                    {"jour": journee.index, "ecart": str(ecart.quantize(Decimal("0.001")))}
                )

    if allergenes_trouves:
        details["allergenes"] = sorted(allergenes_trouves)
    if hors_catalogue:
        details["plats_hors_catalogue"] = sorted(hors_catalogue)

    return ResultatValidation(
        conforme=not echecs,
        controles_en_echec=tuple(sorted(echecs)),
        incident_securite=bool(echecs & CONTROLES_SECURITE),
        details=details,
    )


def proposer_remplacement(
    vivier: Vivier,
    slot: MealSlot,
    kcal_cible: Decimal,
    contraintes: ContraintesUtilisateur,
    *,
    exclus: frozenset[str],
    utilisations: dict[str, int] | None = None,
    poids: dict[str, Decimal] | None = None,
) -> RepasPropose | None:
    """FN-025 — « Proposer autre chose » pour un seul repas.

    Le vivier est déjà filtré (allergies, restrictions) : un remplacement ne
    peut donc pas réintroduire un allergène. `exclus` porte le plat remplacé,
    les plats refusés sur la période et ceux déjà au menu du jour — un plat
    refusé n'est pas reproposé dans la même période. Aucun hasard : à score
    égal, le slug départage, et la même demande donne la même réponse.
    Renvoie `None` quand aucun autre plat ne convient, plutôt que de reproposer
    un plat écarté.
    """
    deja = utilisations or {}
    candidats = [
        (
            scorer(
                plat,
                slot,
                kcal_cible,
                contraintes,
                poids=poids,
                deja_utilise=deja.get(plat.slug, 0),
            ),
            plat,
        )
        for plat in _eligibles(list(vivier.retenus), slot)
        if plat.slug not in exclus
    ]
    if not candidats:
        return None
    candidats.sort(key=lambda c: (-c[0].total, c[1].slug))
    score, plat = candidats[0]
    return RepasPropose(slot=slot, plat=plat, kcal_cible=kcal_cible, score=score)


#: Libellés français des objectifs, pour tout texte destiné à l'utilisateur.
#: Un identifiant technique (`weight_loss`) n'a rien à faire dans une phrase.
LIBELLES_OBJECTIF: dict[str, str] = {
    "weight_loss": "perdre du poids",
    "weight_maintenance": "maintenir votre poids",
    "weight_gain": "prendre du poids",
    "muscle_gain": "prendre du muscle",
    "balanced_diet": "manger équilibré",
    "habit_improvement": "améliorer vos habitudes alimentaires",
}


def justification_de_repli(programme: ProgrammeGenere, contraintes: ContraintesUtilisateur) -> str:
    """FN-021 — le texte produit sans LLM.

    Le fournisseur d'IA ne doit **jamais** bloquer une génération : le
    programme est déjà entièrement décidé, seul son habillage change.
    """
    jours = len(programme.journees)
    repas = sum(len(j.repas) for j in programme.journees)
    return (
        f"Programme de {jours} jour(s), {repas} repas, composé pour un objectif "
        f"« {LIBELLES_OBJECTIF.get(str(contraintes.goal), str(contraintes.goal))} » autour de {contraintes.kcal_target:.0f} kcal par jour. "
        "Les plats ont été choisis parmi ceux du catalogue compatibles avec vos "
        "allergies, vos restrictions et vos préférences."
    )


__all__ = [
    "CONTROLES_SECURITE",
    "ContraintesUtilisateur",
    "EchecGeneration",
    "JourneeProposee",
    "MotifExclusion",
    "PlatCandidat",
    "POIDS_PAR_DEFAUT",
    "ProgrammeGenere",
    "RepasPropose",
    "ReglesComposition",
    "REPARTITION_PAR_DEFAUT",
    "ResultatValidation",
    "ScoreDetaille",
    "TraceCreneau",
    "Vivier",
    "composer",
    "filtrer",
    "LIBELLES_OBJECTIF",
    "justification_de_repli",
    "proposer_remplacement",
    "scorer",
    "valider",
]
