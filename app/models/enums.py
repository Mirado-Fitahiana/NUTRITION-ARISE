"""Énumérations du domaine Nutrition.

Ces valeurs sont celles de la spécification fonctionnelle v2.0. Elles sont
matérialisées en types PostgreSQL natifs (champs scalaires) ou en contrainte
`CHECK` de confinement (champs tableau), afin que la base refuse elle-même une
valeur inconnue — en particulier pour les allergènes (FN-003).
"""

from enum import StrEnum


class Goal(StrEnum):
    """FN-001 — objectifs nutritionnels."""

    WEIGHT_LOSS = "weight_loss"
    WEIGHT_MAINTENANCE = "weight_maintenance"
    WEIGHT_GAIN = "weight_gain"
    MUSCLE_GAIN = "muscle_gain"
    BALANCED_DIET = "balanced_diet"
    HABIT_IMPROVEMENT = "habit_improvement"


class Sex(StrEnum):
    """FN-001. `UNSPECIFIED` applique la formule féminine (minorante)."""

    MALE = "male"
    FEMALE = "female"
    UNSPECIFIED = "unspecified"


class ActivityLevel(StrEnum):
    """FN-038 — niveaux d'activité (facteurs dans `app.services.energy`)."""

    SEDENTARY = "sedentary"
    LIGHTLY_ACTIVE = "lightly_active"
    MODERATELY_ACTIVE = "moderately_active"
    VERY_ACTIVE = "very_active"
    EXTREMELY_ACTIVE = "extremely_active"


class Allergen(StrEnum):
    """FN-003 — les 14 allergènes gérés. 🔴 Toute modification de cette liste
    impose une migration et une revalidation du catalogue."""

    PEANUT = "peanut"
    TREE_NUTS = "tree_nuts"
    MILK = "milk"
    EGG = "egg"
    FISH = "fish"
    CRUSTACEANS = "crustaceans"
    MOLLUSCS = "molluscs"
    SOY = "soy"
    GLUTEN = "gluten"
    SESAME = "sesame"
    MUSTARD = "mustard"
    CELERY = "celery"
    SULPHITES = "sulphites"
    LUPIN = "lupin"


class RestrictionFlag(StrEnum):
    """FN-007 — drapeaux portés par un **ingrédient**.

    Attention, la sémantique n'est pas uniforme, et c'est une source d'erreur
    classique :

    * `VEGETARIAN` / `VEGAN` — l'ingrédient **convient** à ce régime ;
    * `PORK` / `ALCOHOL` / `LACTOSE` / `GLUTEN` — l'ingrédient **en contient**.

    Un plat est donc végétarien si *tous* ses ingrédients le sont, et sans
    lactose si *aucun* n'en contient (`app.services.nutrition`).
    """

    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    PORK = "pork"
    ALCOHOL = "alcohol"
    LACTOSE = "lactose"
    GLUTEN = "gluten"


class RestrictionType(StrEnum):
    """FN-004 — restrictions déclarées par l'utilisateur."""

    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    NO_PORK = "no_pork"
    NO_ALCOHOL = "no_alcohol"
    LACTOSE_FREE = "lactose_free"
    GLUTEN_FREE = "gluten_free"
    RELIGIOUS = "religious"
    FORBIDDEN_FOOD = "forbidden_food"
    CUSTOM = "custom"


class IngredientCategory(StrEnum):
    """FN-007 — catégories du catalogue d'ingrédients."""

    CEREALS = "cereals"
    VEGETABLES = "vegetables"
    FRUITS = "fruits"
    MEATS = "meats"
    FISH = "fish"
    DAIRY = "dairy"
    LEGUMES = "legumes"
    BEVERAGES = "beverages"
    OILS = "oils"
    SPICES = "spices"
    PROCESSED = "processed"


class ReferenceUnit(StrEnum):
    """FN-007 — unité de référence des valeurs nutritionnelles (pour 100)."""

    G = "g"
    ML = "ml"
    UNIT = "unit"


class IngredientStatus(StrEnum):
    ACTIVE = "active"
    DRAFT = "draft"
    ARCHIVED = "archived"


class DishStatus(StrEnum):
    """FN-008 — cycle de vie d'un plat."""

    DRAFT = "draft"
    PENDING_VALIDATION = "pending_validation"
    VALIDATED = "validated"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class MealType(StrEnum):
    """FN-008 — types de repas auxquels un plat convient."""

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class MealSlot(StrEnum):
    """FN-023 — créneaux d'une journée générée."""

    BREAKFAST = "breakfast"
    MORNING_SNACK = "morning_snack"
    LUNCH = "lunch"
    AFTERNOON_SNACK = "afternoon_snack"
    DINNER = "dinner"


class DishTag(StrEnum):
    """FN-009. Les tags dérivables sont calculés depuis les ingrédients ; seuls
    les tags subjectifs sont saisis (cf. `DERIVED_TAGS`)."""

    ECONOMICAL = "economical"
    QUICK = "quick"
    LOCAL = "local"
    HIGH_PROTEIN = "high_protein"
    LOW_CARB = "low_carb"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    LACTOSE_FREE = "lactose_free"
    GLUTEN_FREE = "gluten_free"
    MUSCLE_GAIN = "muscle_gain"
    WEIGHT_LOSS = "weight_loss"
    FAMILY_MEAL = "family_meal"
    MAKE_AHEAD = "make_ahead"
    SEASONAL = "seasonal"


#: Tags calculés depuis les ingrédients — jamais saisis à la main (FN-009).
DERIVED_TAGS: frozenset[DishTag] = frozenset(
    {
        DishTag.VEGETARIAN,
        DishTag.VEGAN,
        DishTag.LACTOSE_FREE,
        DishTag.GLUTEN_FREE,
        DishTag.HIGH_PROTEIN,
        DishTag.LOW_CARB,
    }
)


class CostClass(StrEnum):
    """FN-005 — classe de coût indicative, utilisée au lot 2 en l'absence de
    données de prix réelles."""

    ECONOMICAL = "economical"
    MEDIUM = "medium"
    HIGH = "high"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class CostConfidence(StrEnum):
    """FN-005 — fiabilité du chiffrage : estimé (lot 2) ou observé (lot 3)."""

    ESTIMATED = "estimated"
    OBSERVED = "observed"


class VendorType(StrEnum):
    """FN-030 · D-10 — nature d'un point de vente.

    `OFFICIAL_REFERENCE` n'est pas un commerce : c'est le porteur des séries
    publiques (INSTAT, mercuriale). Le distinguer évite qu'un indice régional
    soit présenté comme le prix relevé chez un commerçant précis.
    """

    SUPERMARKET = "supermarket"
    MARKET = "market"
    GROCERY = "grocery"
    OFFICIAL_REFERENCE = "official_reference"


class PriceSource(StrEnum):
    """D-08 — la méthode de collecte est un simple attribut de l'observation.

    C'est ce qui permet d'ajouter le scraping plus tard sans réécrire le
    pipeline, et de pondérer la confiance selon l'origine.
    """

    MANUAL = "manual"
    OFFICIAL = "official"
    COMMUNITY = "community"
    SCRAPING = "scraping"


class PriceFreshness(StrEnum):
    """FN-016 — fraîcheur d'un relevé. Les seuils sont **configurables** ;
    les valeurs par défaut vivent dans `app.services.pricing`.

    `OBSOLETE` est exclu du budget : un prix de plus de 45 jours ne doit pas
    servir à chiffrer une liste de courses.
    """

    RECENT = "recent"
    STALE = "stale"
    OBSOLETE = "obsolete"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class MealPlanStatus(StrEnum):
    """FN-023 / FN-001 — un programme actif devient `obsolete` quand le profil
    change ; il n'est jamais supprimé."""

    GENERATING = "generating"
    ACTIVE = "active"
    OBSOLETE = "obsolete"
    ARCHIVED = "archived"
    FAILED = "failed"


class JobStatus(StrEnum):
    """FN-023 — états de la génération asynchrone."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OperationStatus(StrEnum):
    """Cycle de vie d'une exécution longue de la plateforme de pilotage
    (collecte, rejeu, indexation, évaluation).

    `INTERRUPTED` n'est pas un échec : le processus s'est arrêté en cours de
    route (redémarrage d'uvicorn en développement). La cause n'est pas la même,
    la reprise non plus.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TrackedStatus(StrEnum):
    """FN-031 — suivi d'un repas par l'utilisateur."""

    PENDING = "pending"
    FOLLOWED = "followed"
    SKIPPED = "skipped"
    REPLACED = "replaced"


class FeedbackType(StrEnum):
    """FN-032. `ALLERGY_ISSUE` est traité comme un incident, jamais comme un
    simple avis."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPLACED = "replaced"
    TOO_EXPENSIVE = "too_expensive"
    INGREDIENT_UNAVAILABLE = "ingredient_unavailable"
    DISLIKED = "disliked"
    TOO_DIFFICULT = "too_difficult"
    TOO_LONG = "too_long"
    ALLERGY_ISSUE = "allergy_issue"
    OTHER = "other"


class ValidationOutcome(StrEnum):
    """FN-022 — issues de la validation finale."""

    VALIDATED = "validated"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    REGENERATION_REQUESTED = "regeneration_requested"


class AuditResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
