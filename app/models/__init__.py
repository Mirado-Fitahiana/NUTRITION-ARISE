"""Modèles du service Nutrition.

Ce module importe l'ensemble des tables : c'est lui qu'Alembic charge pour
comparer le schéma cible à la base. Un modèle absent d'ici est un modèle absent
des migrations.

Le schéma couvre les **lots 1 et 2** (critère de fin du lot 1), plus la table
`dish_embeddings` du lot 4, incluse dès maintenant pour éviter une migration
structurelle ultérieure.
"""

from app.models.audit import AuditAction, AuditLog
from app.models.base import Base
from app.models.catalog import (
    Dish,
    DishAllergen,
    DishEmbedding,
    DishIngredient,
    DishStep,
    DishTagLink,
    Ingredient,
    IngredientAlias,
    IngredientUnitConversion,
)
from app.models.configuration import AppSetting, ScoringWeightSet
from app.models.planning import (
    GenerationJob,
    MealPlan,
    MealPlanDay,
    MealPlanMeal,
    PlanValidation,
    RecommendationCandidate,
    RecommendationRun,
    UserMealFeedback,
)
from app.models.profile import (
    DietaryPreference,
    DietaryRestriction,
    DislikedIngredient,
    FavoriteIngredient,
    NutritionProfile,
    NutritionProfileHistory,
    NutritionTarget,
    UserAllergy,
)

__all__ = [
    "AppSetting",
    "AuditAction",
    "AuditLog",
    "Base",
    "DietaryPreference",
    "DietaryRestriction",
    "Dish",
    "DishAllergen",
    "DishEmbedding",
    "DishIngredient",
    "DishStep",
    "DishTagLink",
    "DislikedIngredient",
    "FavoriteIngredient",
    "GenerationJob",
    "Ingredient",
    "IngredientAlias",
    "IngredientUnitConversion",
    "MealPlan",
    "MealPlanDay",
    "MealPlanMeal",
    "NutritionProfile",
    "NutritionProfileHistory",
    "NutritionTarget",
    "PlanValidation",
    "RecommendationCandidate",
    "RecommendationRun",
    "ScoringWeightSet",
    "UserAllergy",
    "UserMealFeedback",
]
