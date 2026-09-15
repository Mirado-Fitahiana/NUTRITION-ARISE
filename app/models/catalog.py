"""Catalogue d'ingrédients et de plats (FN-007 → FN-009).

Le catalogue `dishes` est la **source de vérité unique** du concept « plat »
pour tout l'écosystème ARISE (D-01). La table `menus` de NestJS le référencera
progressivement, sans être migrée (D-02, §4.5).

Deux invariants structurent ce module :

* **D-11** — les valeurs nutritionnelles d'un plat sont *calculées* depuis ses
  ingrédients, jamais saisies ; les colonnes correspondantes portent le suffixe
  `_portion` et sont recalculées par `app.services.nutrition`.
* **FN-003** — un ingrédient dont les allergènes ne sont pas vérifiés rend tout
  plat qui l'utilise non publiable. La vérification est l'existence d'une
  source *et* d'un validateur, pas l'absence d'allergène.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, REAL
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, enum_array_check, pg_enum, uuid_pk
from app.models.enums import (
    Allergen,
    CostClass,
    Difficulty,
    DishStatus,
    DishTag,
    Goal,
    IngredientCategory,
    IngredientStatus,
    MealType,
    ReferenceUnit,
    RestrictionFlag,
    RestrictionType,
)

# Identifiants d'acteurs ARISE (claim `sub`) : jamais de clé étrangère (D-04).
ACTOR_ID_LEN = 128


class Ingredient(Base, TimestampMixin):
    """FN-007 — brique de base du catalogue."""

    __tablename__ = "ingredients"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    category: Mapped[IngredientCategory] = mapped_column(
        pg_enum(IngredientCategory, "ingredient_category"), nullable=False
    )
    reference_unit: Mapped[ReferenceUnit] = mapped_column(
        pg_enum(ReferenceUnit, "reference_unit"), nullable=False
    )

    # --- Valeurs nutritionnelles pour 100 g / 100 ml / 1 unité ---
    kcal_100: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    protein_100: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    carbs_100: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    fat_100: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    fiber_100: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False, default=0)
    #: Table de composition d'origine (FAO/INFOODS, CIQUAL, USDA). Obligatoire :
    #: aucune valeur ne doit être inventée.
    nutrition_source: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- Allergènes (FN-003 · 🔴) ---
    allergens: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    allergen_source: Mapped[str | None] = mapped_column(String(255))
    allergen_verified_by: Mapped[str | None] = mapped_column(String(ACTOR_ID_LEN))
    allergen_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Restrictions et disponibilité ---
    restriction_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    locally_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Mois de saison (1–12). Vide = disponible toute l'année.
    seasonality: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, default=list
    )
    #: Densité, indispensable aux conversions poids ↔ volume du lot 3 (FN-012).
    density_g_per_ml: Mapped[float | None] = mapped_column(Numeric(8, 4))

    image_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[IngredientStatus] = mapped_column(
        pg_enum(IngredientStatus, "ingredient_status"),
        nullable=False,
        default=IngredientStatus.ACTIVE,
    )

    aliases: Mapped[list["IngredientAlias"]] = relationship(
        back_populates="ingredient", cascade="all, delete-orphan"
    )
    unit_conversions: Mapped[list["IngredientUnitConversion"]] = relationship(
        back_populates="ingredient", cascade="all, delete-orphan"
    )

    @property
    def is_allergen_verified(self) -> bool:
        """FN-003 — un ingrédient sans allergène reste *non vérifié* tant qu'un
        validateur ne l'a pas signé. L'absence d'allergène est une affirmation,
        pas une donnée manquante."""
        return (
            self.allergen_source is not None
            and self.allergen_verified_by is not None
            and self.allergen_verified_at is not None
        )

    __table_args__ = (
        enum_array_check("allergens", Allergen, "allergens_known"),
        enum_array_check("restriction_flags", RestrictionFlag, "restriction_flags_known"),
        CheckConstraint(
            "kcal_100 >= 0 AND protein_100 >= 0 AND carbs_100 >= 0"
            " AND fat_100 >= 0 AND fiber_100 >= 0",
            name="nutrition_not_negative",
        ),
        CheckConstraint(
            "density_g_per_ml IS NULL OR density_g_per_ml > 0", name="density_positive"
        ),
        CheckConstraint(
            "seasonality <@ ARRAY[1,2,3,4,5,6,7,8,9,10,11,12]::smallint[]",
            name="seasonality_months",
        ),
        # La vérification allergène est atomique : les trois champs vont ensemble.
        CheckConstraint(
            "(allergen_source IS NULL AND allergen_verified_by IS NULL"
            " AND allergen_verified_at IS NULL)"
            " OR (allergen_source IS NOT NULL AND allergen_verified_by IS NOT NULL"
            " AND allergen_verified_at IS NOT NULL)",
            name="allergen_verification_complete",
        ),
        Index("ix_ingredients_allergens", "allergens", postgresql_using="gin"),
        Index("ix_ingredients_category_status", "category", "status"),
    )


class IngredientAlias(Base):
    """FN-007 — synonymes, pour réconcilier imports et collectes (lots 3–4)."""

    __tablename__ = "ingredient_aliases"

    id: Mapped[uuid.UUID] = uuid_pk()
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Langue de l'alias : `mg` (malagasy), `fr`, `en`.
    language: Mapped[str] = mapped_column(String(5), nullable=False, default="fr")

    ingredient: Mapped[Ingredient] = relationship(back_populates="aliases")

    __table_args__ = (UniqueConstraint("ingredient_id", "alias"),)


class IngredientUnitConversion(Base):
    """FN-012 — conversion **par ingrédient**, jamais générique.

    Une conversion locale (kapoaka de riz, botte de brèdes, tas de tomates) est
    *mesurée* puis validée. Une conversion absente doit bloquer le chiffrage et
    le signaler, jamais inventer un facteur.
    """

    __tablename__ = "ingredient_unit_conversions"

    id: Mapped[uuid.UUID] = uuid_pk()
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    from_unit: Mapped[str] = mapped_column(String(40), nullable=False)
    to_unit: Mapped[str] = mapped_column(String(40), nullable=False)
    factor: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    #: Origine du facteur : `measured`, `reference_table`, `manufacturer`.
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ingredient: Mapped[Ingredient] = relationship(back_populates="unit_conversions")

    __table_args__ = (
        UniqueConstraint("ingredient_id", "from_unit", "to_unit"),
        CheckConstraint("factor > 0", name="factor_positive"),
        CheckConstraint("from_unit <> to_unit", name="units_differ"),
    )


class Dish(Base, TimestampMixin):
    """FN-008 — plat du catalogue. Source de vérité unique (D-01)."""

    __tablename__ = "dishes"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String(500))

    meal_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    origin: Mapped[str | None] = mapped_column(String(120))
    prep_time_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cook_time_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    difficulty: Mapped[Difficulty] = mapped_column(
        pg_enum(Difficulty, "difficulty"), nullable=False, default=Difficulty.EASY
    )
    servings: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- Valeurs nutritionnelles par portion — CALCULÉES (D-11) ---
    kcal_portion: Mapped[float | None] = mapped_column(Numeric(8, 2))
    protein_portion: Mapped[float | None] = mapped_column(Numeric(8, 2))
    carbs_portion: Mapped[float | None] = mapped_column(Numeric(8, 2))
    fat_portion: Mapped[float | None] = mapped_column(Numeric(8, 2))
    fiber_portion: Mapped[float | None] = mapped_column(Numeric(8, 2))
    nutrition_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Coût indicatif, seul repère budgétaire du lot 2 (FN-005) ---
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 2))
    cost_class: Mapped[CostClass] = mapped_column(
        pg_enum(CostClass, "cost_class"), nullable=False, default=CostClass.MEDIUM
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="MGA")

    # --- Compatibilités dérivées des ingrédients ---
    compatible_goals: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    #: Restrictions utilisateur (`RestrictionType`) que le plat satisfait —
    #: `vegetarian`, `lactose_free`, … — et non les drapeaux des ingrédients.
    compatible_restrictions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )

    # --- Cycle de vie et traçabilité de responsabilité (FN-008) ---
    status: Mapped[DishStatus] = mapped_column(
        pg_enum(DishStatus, "dish_status"), nullable=False, default=DishStatus.DRAFT
    )
    author_id: Mapped[str | None] = mapped_column(String(ACTOR_ID_LEN))
    #: Nutritionniste ayant validé. C'est la traçabilité en cas d'incident.
    validated_by: Mapped[str | None] = mapped_column(String(ACTOR_ID_LEN))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    ingredients: Mapped[list["DishIngredient"]] = relationship(
        back_populates="dish", cascade="all, delete-orphan"
    )
    steps: Mapped[list["DishStep"]] = relationship(
        back_populates="dish", cascade="all, delete-orphan", order_by="DishStep.step_number"
    )
    tags: Mapped[list["DishTagLink"]] = relationship(
        back_populates="dish", cascade="all, delete-orphan"
    )
    allergens: Mapped[list["DishAllergen"]] = relationship(
        back_populates="dish", cascade="all, delete-orphan"
    )

    __table_args__ = (
        enum_array_check("meal_types", MealType, "meal_types_known"),
        enum_array_check("compatible_goals", Goal, "compatible_goals_known"),
        enum_array_check(
            "compatible_restrictions", RestrictionType, "compatible_restrictions_known"
        ),
        CheckConstraint("servings > 0", name="servings_positive"),
        CheckConstraint("cardinality(meal_types) > 0", name="meal_types_not_empty"),
        CheckConstraint(
            "prep_time_min >= 0 AND cook_time_min >= 0", name="times_not_negative"
        ),
        CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0", name="cost_not_negative"
        ),
        # FN-008 : la publication exige un validateur.
        CheckConstraint(
            "status <> 'published' OR (validated_by IS NOT NULL AND validated_at IS NOT NULL)",
            name="published_requires_validation",
        ),
        # FN-008 : le validateur ne peut pas être l'auteur.
        CheckConstraint(
            "validated_by IS NULL OR author_id IS NULL OR validated_by <> author_id",
            name="validator_differs_from_author",
        ),
        # D-11 : un plat publié a nécessairement des valeurs calculées.
        CheckConstraint(
            "status <> 'published' OR nutrition_computed_at IS NOT NULL",
            name="published_requires_computed_nutrition",
        ),
        Index("ix_dishes_status_meal_types", "status", "meal_types"),
        Index("ix_dishes_meal_types_gin", "meal_types", postgresql_using="gin"),
    )


class DishIngredient(Base):
    """FN-008 — composition d'un plat, pour `servings` portions."""

    __tablename__ = "dish_ingredients"

    id: Mapped[uuid.UUID] = uuid_pk()
    dish_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dishes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredients.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(255))

    dish: Mapped[Dish] = relationship(back_populates="ingredients")
    ingredient: Mapped[Ingredient] = relationship()

    __table_args__ = (
        UniqueConstraint("dish_id", "ingredient_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )


class DishStep(Base):
    """FN-008 — étapes de préparation."""

    __tablename__ = "dish_steps"

    id: Mapped[uuid.UUID] = uuid_pk()
    dish_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dishes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    duration_min: Mapped[int | None] = mapped_column(Integer)

    dish: Mapped[Dish] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("dish_id", "step_number"),
        CheckConstraint("step_number > 0", name="step_number_positive"),
    )


class DishTagLink(Base):
    """FN-009. `is_derived` distingue les tags calculés depuis les ingrédients
    des tags subjectifs saisis par un administrateur."""

    __tablename__ = "dish_tags"

    dish_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dishes.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[DishTag] = mapped_column(pg_enum(DishTag, "dish_tag"), primary_key=True)
    is_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    dish: Mapped[Dish] = relationship(back_populates="tags")


class DishAllergen(Base):
    """FN-008 · 🔴 Table **dérivée** : union des allergènes des ingrédients.

    Recalculée à chaque modification. Un administrateur peut *ajouter* un
    allergène (`is_manual`), jamais retirer un allergène propagé.
    """

    __tablename__ = "dish_allergens"

    dish_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dishes.id", ondelete="CASCADE"), primary_key=True
    )
    allergen: Mapped[Allergen] = mapped_column(
        pg_enum(Allergen, "allergen"), primary_key=True
    )
    #: Vrai si ajouté à la main ; un allergène propagé ne peut pas être supprimé.
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    dish: Mapped[Dish] = relationship(back_populates="allergens")


class DishEmbedding(Base):
    """FN-018 — recherche vectorielle, **différée au lot 4**.

    La table figure au schéma dès le lot 1 pour éviter une migration
    structurelle ultérieure (§7 de la grille d'analyse). `pgvector` n'étant pas
    disponible sur l'instance PostgreSQL actuelle, le vecteur est stocké en
    `real[]` ; le lot 4 se contentera d'un `ALTER TYPE` vers `vector` une fois
    l'extension installée, sans nouvelle table ni nouvelle clé étrangère.
    """

    __tablename__ = "dish_embeddings"

    dish_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dishes.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    embedding_version: Mapped[str | None] = mapped_column(String(16))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Dish",
    "DishAllergen",
    "DishEmbedding",
    "DishIngredient",
    "DishStep",
    "DishTagLink",
    "Ingredient",
    "IngredientAlias",
    "IngredientUnitConversion",
]
