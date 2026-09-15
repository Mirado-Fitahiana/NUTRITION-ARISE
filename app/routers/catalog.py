"""Catalogue d'ingrédients et de plats (FN-007 → FN-009).

Deux routeurs, deux publics :

* **lecture publique** — un utilisateur authentifié ne voit que des plats
  `published`. C'est la traduction directe de « seul un plat publié peut être
  recommandé » (FN-008) : si un brouillon n'est pas visible, il ne peut pas être
  recommandé par erreur ;
* **administration** — saisie et cycle de vie, réservés à l'administrateur et au
  nutritionniste, chaque transition étant journalisée.

Le cycle de vie n'a **pas** de route générique « changer le statut ». Chaque
transition a son point d'entrée, ses conditions et son entrée de journal. Une
route générique aurait fini par accepter `{"status": "published"}` sans passer
par le contrôle de FN-003.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import (
    Principal,
    current_user,
    require_catalog_editor,
    require_validator,
)
from app.core.database import get_session
from app.core.errors import ErrorCode, NutritionError
from app.models.catalog import (
    Dish,
    DishIngredient,
    DishStep,
    Ingredient,
    IngredientAlias,
    IngredientUnitConversion,
)
from app.models.enums import AuditResult, DishStatus, IngredientStatus, MealType
from app.schemas.catalog import (
    AliasOut,
    ConversionOut,
    DishIn,
    DishIngredientOut,
    DishOut,
    DishPage,
    IngredientIn,
    IngredientOut,
    IngredientPage,
    NutritionOut,
    RejectIn,
    VerifyAllergensIn,
)
from app.services import audit
from app.services.audit import AuditAction
from app.services.catalog import (
    apply_derived,
    can_transition,
    publication_blockers,
    recompute,
)
from app.services.units import UnitConversionError

public = APIRouter(prefix="/api/v1", tags=["catalogue"])
admin = APIRouter(
    prefix="/api/v1/admin",
    tags=["catalogue (administration)"],
    dependencies=[Depends(require_catalog_editor)],
)


# --------------------------------------------------------------------------
# Chargement et sérialisation
# --------------------------------------------------------------------------


def _ingredient_query() -> Select:
    return select(Ingredient).options(
        selectinload(Ingredient.aliases),
        selectinload(Ingredient.unit_conversions),
    )


def _dish_query() -> Select:
    return select(Dish).options(
        selectinload(Dish.ingredients)
        .selectinload(DishIngredient.ingredient)
        .selectinload(Ingredient.unit_conversions),
        selectinload(Dish.steps),
        selectinload(Dish.tags),
        selectinload(Dish.allergens),
    )


def ingredient_out(row: Ingredient) -> IngredientOut:
    return IngredientOut(
        id=row.id,
        slug=row.slug,
        name=row.name,
        category=row.category,
        reference_unit=row.reference_unit,
        kcal_100=row.kcal_100,
        protein_100=row.protein_100,
        carbs_100=row.carbs_100,
        fat_100=row.fat_100,
        fiber_100=row.fiber_100,
        nutrition_source=row.nutrition_source,
        allergens=list(row.allergens),
        allergen_source=row.allergen_source,
        allergen_verified_by=row.allergen_verified_by,
        allergen_verified_at=row.allergen_verified_at,
        allergen_verified=row.is_allergen_verified,
        restriction_flags=list(row.restriction_flags),
        locally_available=row.locally_available,
        seasonality=list(row.seasonality),
        density_g_per_ml=row.density_g_per_ml,
        image_url=row.image_url,
        status=row.status,
        aliases=[AliasOut.model_validate(a) for a in row.aliases],
        unit_conversions=[ConversionOut.model_validate(c) for c in row.unit_conversions],
    )


def dish_out(row: Dish, *, with_blockers: bool = False) -> DishOut:
    """`with_blockers` n'est vrai que côté administration : recalculer les
    motifs de blocage pour chaque plat d'une liste publique serait du travail
    pur perte."""
    blockers: list[str] = []
    if with_blockers:
        try:
            blockers = publication_blockers(row)
        except UnitConversionError as exc:
            blockers = [str(exc)]

    return DishOut(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        image_url=row.image_url,
        meal_types=list(row.meal_types),
        origin=row.origin,
        prep_time_min=row.prep_time_min,
        cook_time_min=row.cook_time_min,
        difficulty=row.difficulty,
        servings=row.servings,
        nutrition=NutritionOut(
            kcal=row.kcal_portion,
            protein_g=row.protein_portion,
            carbs_g=row.carbs_portion,
            fat_g=row.fat_portion,
            fiber_g=row.fiber_portion,
        ),
        nutrition_computed_at=row.nutrition_computed_at,
        estimated_cost=row.estimated_cost,
        cost_class=row.cost_class,
        currency=row.currency,
        allergens=sorted(a.allergen.value for a in row.allergens),
        compatible_goals=list(row.compatible_goals),
        compatible_restrictions=list(row.compatible_restrictions),
        tags_derived=sorted(t.tag.value for t in row.tags if t.is_derived),
        tags_manual=sorted(t.tag.value for t in row.tags if not t.is_derived),
        status=row.status,
        author_id=row.author_id,
        validated_by=row.validated_by,
        validated_at=row.validated_at,
        published_at=row.published_at,
        rejection_reason=row.rejection_reason,
        ingredients=[
            DishIngredientOut(
                ingredient=link.ingredient.slug,
                name=link.ingredient.name,
                quantity=link.quantity,
                unit=link.unit,
                note=link.note,
                allergen_verified=link.ingredient.is_allergen_verified,
            )
            for link in sorted(row.ingredients, key=lambda i: i.display_order)
        ],
        steps=[s.instruction for s in sorted(row.steps, key=lambda s: s.step_number)],
        publication_blockers=blockers,
    )


async def _get_ingredient(session: AsyncSession, key: str) -> Ingredient:
    """Accepte un slug ou un UUID : la file de validation manipule des slugs,
    le mobile des identifiants."""
    query = _ingredient_query()
    try:
        query = query.where(Ingredient.id == uuid.UUID(key))
    except ValueError:
        query = query.where(Ingredient.slug == key)
    row = await session.scalar(query)
    if row is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"Aucun ingrédient « {key} ».",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return row


async def _get_dish(session: AsyncSession, key: str) -> Dish:
    query = _dish_query()
    try:
        query = query.where(Dish.id == uuid.UUID(key))
    except ValueError:
        query = query.where(Dish.slug == key)
    row = await session.scalar(query)
    if row is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"Aucun plat « {key} ».",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return row


# --------------------------------------------------------------------------
# Lecture publique
# --------------------------------------------------------------------------


@public.get(
    "/ingredients", response_model=IngredientPage, summary="FN-007 — lister les ingrédients"
)
async def list_ingredients(
    q: str | None = Query(default=None, max_length=120, description="Nom ou slug"),
    category: str | None = None,
    verified_only: bool = Query(
        default=False, description="Seulement les ingrédients dont les allergènes sont signés"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> IngredientPage:
    query = _ingredient_query().where(Ingredient.status == IngredientStatus.ACTIVE)
    if q:
        pattern = f"%{q.lower()}%"
        query = query.where(
            func.lower(Ingredient.name).like(pattern)
            | func.lower(Ingredient.slug).like(pattern)
        )
    if category:
        query = query.where(Ingredient.category == category)
    if verified_only:
        query = query.where(
            Ingredient.allergen_source.is_not(None),
            Ingredient.allergen_verified_by.is_not(None),
            Ingredient.allergen_verified_at.is_not(None),
        )

    total = await session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = (
        await session.scalars(query.order_by(Ingredient.name).limit(limit).offset(offset))
    ).all()
    return IngredientPage(
        total=total or 0, limit=limit, offset=offset,
        items=[ingredient_out(r) for r in rows],
    )


@public.get(
    "/ingredients/{key}", response_model=IngredientOut, summary="FN-007 — lire un ingrédient"
)
async def read_ingredient(
    key: str,
    _: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> IngredientOut:
    return ingredient_out(await _get_ingredient(session, key))


@public.get("/dishes", response_model=DishPage, summary="FN-008 — lister les plats publiés")
async def list_dishes(
    q: str | None = Query(default=None, max_length=120),
    meal_type: MealType | None = None,
    tag: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DishPage:
    """Ne renvoie que des plats `published` : un brouillon invisible ne peut pas
    être recommandé par accident."""
    query = _dish_query().where(Dish.status == DishStatus.PUBLISHED)
    if q:
        pattern = f"%{q.lower()}%"
        query = query.where(
            func.lower(Dish.name).like(pattern) | func.lower(Dish.slug).like(pattern)
        )
    if meal_type:
        query = query.where(Dish.meal_types.any(meal_type.value))
    if tag:
        query = query.where(Dish.compatible_restrictions.any(tag))

    total = await session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = (
        await session.scalars(query.order_by(Dish.name).limit(limit).offset(offset))
    ).all()
    return DishPage(
        total=total or 0, limit=limit, offset=offset, items=[dish_out(r) for r in rows]
    )


@public.get("/dishes/{key}", response_model=DishOut, summary="FN-008 — lire un plat publié")
async def read_dish(
    key: str,
    _: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    dish = await _get_dish(session, key)
    if dish.status is not DishStatus.PUBLISHED:
        # Même réponse qu'un plat inexistant : l'existence d'un brouillon n'est
        # pas une information publique.
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"Aucun plat « {key} ».",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return dish_out(dish)


# --------------------------------------------------------------------------
# Administration — ingrédients
# --------------------------------------------------------------------------


def _write_ingredient(row: Ingredient, payload: IngredientIn) -> None:
    for field in (
        "name", "category", "reference_unit", "kcal_100", "protein_100",
        "carbs_100", "fat_100", "fiber_100", "nutrition_source",
        "locally_available", "density_g_per_ml", "image_url", "status",
    ):
        setattr(row, field, getattr(payload, field))
    row.restriction_flags = [f.value for f in payload.restriction_flags]
    row.seasonality = list(payload.seasonality)


async def _clear_children(session: AsyncSession, *collections: list) -> None:
    """Vide des collections **déjà chargées**, puis force le `flush`.

    Le `flush` intermédiaire n'est pas cosmétique : sans lui SQLAlchemy émet les
    `INSERT` avant les `DELETE`, et les contraintes d'unicité sautent. Ne jamais
    appeler sur un objet neuf : `.clear()` y déclencherait un chargement
    paresseux, interdit dans une session asynchrone.
    """
    for collection in collections:
        collection.clear()
    await session.flush()


@admin.get(
    "/ingredients", response_model=IngredientPage, summary="FN-007 — inventaire complet"
)
async def admin_list_ingredients(
    unverified_only: bool = Query(
        default=False,
        description="FN-035 — ingrédients dont les allergènes ne sont pas signés",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> IngredientPage:
    query = _ingredient_query()
    if unverified_only:
        query = query.where(
            Ingredient.allergen_source.is_(None)
            | Ingredient.allergen_verified_by.is_(None)
            | Ingredient.allergen_verified_at.is_(None)
        )
    total = await session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = (
        await session.scalars(query.order_by(Ingredient.name).limit(limit).offset(offset))
    ).all()
    return IngredientPage(
        total=total or 0, limit=limit, offset=offset,
        items=[ingredient_out(r) for r in rows],
    )


@admin.post(
    "/ingredients",
    response_model=IngredientOut,
    status_code=status.HTTP_201_CREATED,
    summary="FN-007 — créer un ingrédient",
)
async def create_ingredient(
    payload: IngredientIn,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> IngredientOut:
    if await session.scalar(select(Ingredient).where(Ingredient.slug == payload.slug)):
        raise NutritionError(
            ErrorCode.CONFLICT,
            message=f"Un ingrédient porte déjà le slug « {payload.slug} ».",
            http_status=status.HTTP_409_CONFLICT,
        )

    row = Ingredient(id=uuid.uuid4(), slug=payload.slug)
    _write_ingredient(row, payload)
    # Un ingrédient naît non vérifié : la signature est un acte distinct.
    row.allergens = []
    _extend_children(row, payload)
    session.add(row)
    await session.flush()

    await audit.record(
        session, AuditAction.INGREDIENT_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="ingredient", resource_id=row.id,
        details={"operation": "create", "slug": row.slug},
    )
    return ingredient_out(row)


def _extend_children(row: Ingredient, payload: IngredientIn) -> None:
    """Ajoute alias et conversions. N'efface rien : l'appelant s'en charge
    quand l'objet existe déjà."""
    row.aliases.extend(
        [
            IngredientAlias(id=uuid.uuid4(), alias=a.alias, language=a.language)
            for a in payload.aliases
        ]
    )
    row.unit_conversions.extend(
        [
            IngredientUnitConversion(
                id=uuid.uuid4(),
                from_unit=c.from_unit,
                to_unit=c.to_unit,
                factor=c.factor,
                source=c.source,
                measured_at=(
                    datetime.combine(c.measured_at, datetime.min.time(), tzinfo=UTC)
                    if c.measured_at
                    else None
                ),
            )
            for c in payload.unit_conversions
        ]
    )


@admin.put(
    "/ingredients/{key}", response_model=IngredientOut, summary="FN-007 — modifier un ingrédient"
)
async def update_ingredient(
    key: str,
    payload: IngredientIn,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> IngredientOut:
    row = await _get_ingredient(session, key)
    if payload.slug != row.slug:
        raise NutritionError(
            ErrorCode.CONFLICT,
            message="Le slug d'un ingrédient ne se modifie pas : c'est la clé "
            "fonctionnelle du catalogue (D-12).",
            http_status=status.HTTP_409_CONFLICT,
        )

    _write_ingredient(row, payload)
    await _clear_children(session, row.aliases, row.unit_conversions)
    _extend_children(row, payload)
    await session.flush()

    # D-11 : les plats qui l'utilisent sont recalculés immédiatement. Ne pas le
    # faire laisserait des valeurs nutritionnelles fausses en base jusqu'à la
    # prochaine modification du plat.
    touched = await _recompute_dishes_using(session, row.id)

    await audit.record(
        session, AuditAction.INGREDIENT_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="ingredient", resource_id=row.id,
        details={"operation": "update", "slug": row.slug, "dishes_recomputed": touched},
    )
    return ingredient_out(row)


async def _recompute_dishes_using(session: AsyncSession, ingredient_id) -> int:
    """Recalcule tous les plats utilisant cet ingrédient (D-11).

    Un plat dont la conversion devient impossible n'est pas silencieusement
    laissé de côté : il est dépublié, parce qu'un plat publié dont on ne sait
    plus calculer les valeurs est exactement ce que D-11 interdit.
    """
    dishes = (
        await session.scalars(
            _dish_query().where(
                Dish.id.in_(
                    select(DishIngredient.dish_id).where(
                        DishIngredient.ingredient_id == ingredient_id
                    )
                )
            )
        )
    ).all()

    for dish in dishes:
        try:
            apply_derived(dish, recompute(dish))
        except UnitConversionError:
            if dish.status is DishStatus.PUBLISHED:
                dish.status = DishStatus.PENDING_VALIDATION
                dish.published_at = None
    await session.flush()
    return len(dishes)


@admin.post(
    "/ingredients/{key}/verify-allergens",
    response_model=IngredientOut,
    summary="FN-003 · 🔴 signer la grille allergènes",
    dependencies=[Depends(require_validator)],
)
async def verify_allergens(
    key: str,
    payload: VerifyAllergensIn,
    principal: Principal = Depends(require_validator),
    session: AsyncSession = Depends(get_session),
) -> IngredientOut:
    """Réservé au nutritionniste. C'est l'acte qui engage sa responsabilité :
    son identifiant est enregistré, et c'est lui qui débloque la publication des
    plats utilisant cet ingrédient."""
    row = await _get_ingredient(session, key)

    row.allergens = [a.value for a in payload.allergens]
    row.allergen_source = payload.source
    row.allergen_verified_by = principal.external_user_id
    row.allergen_verified_at = datetime.now(UTC)
    await session.flush()

    touched = await _recompute_dishes_using(session, row.id)

    await audit.record(
        session, AuditAction.INGREDIENT_ALLERGENS_VERIFIED,
        external_user_id=principal.external_user_id,
        resource_type="ingredient", resource_id=row.id,
        details={
            "slug": row.slug,
            # Le nombre, pas la liste : un allergène est une donnée de santé.
            "allergen_count": len(payload.allergens),
            "dishes_recomputed": touched,
        },
    )
    return ingredient_out(row)


# --------------------------------------------------------------------------
# Administration — plats
# --------------------------------------------------------------------------


async def _apply_dish(
    session: AsyncSession, dish: Dish, payload: DishIn, *, is_new: bool
) -> None:
    """Écrit la composition puis recalcule les valeurs dérivées.

    `is_new` n'est pas un détail : sur un objet qui vient d'être créé, vider
    une collection déclencherait un chargement paresseux, interdit dans une
    session asynchrone. Les ingrédients sont rattachés par **objet** et non
    par identifiant, pour que le recalcul qui suit n'ait rien à recharger.
    """
    slugs = [e.ingredient for e in payload.ingredients]
    rows = (
        await session.scalars(
            _ingredient_query().where(Ingredient.slug.in_(sorted(set(slugs))))
        )
    ).all()
    by_slug = {r.slug: r for r in rows}
    missing = sorted(set(slugs) - set(by_slug))
    if missing:
        raise NutritionError(
            ErrorCode.VALIDATION_ERROR,
            message="Ingrédient(s) inconnu(s) : " + ", ".join(missing),
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    for field in (
        "name", "description", "image_url", "origin", "prep_time_min",
        "cook_time_min", "difficulty", "servings", "estimated_cost",
        "cost_class", "currency",
    ):
        setattr(dish, field, getattr(payload, field))
    dish.meal_types = [m.value for m in payload.meal_types]
    dish.compatible_goals = [g.value for g in payload.compatible_goals]

    from app.models.catalog import DishTagLink

    if not is_new:
        await _clear_children(session, dish.ingredients, dish.steps, dish.tags)

    dish.ingredients.extend(
        DishIngredient(
            id=uuid.uuid4(),
            ingredient=by_slug[e.ingredient],
            quantity=e.quantity,
            unit=e.unit,
            display_order=order,
            note=e.note,
        )
        for order, e in enumerate(payload.ingredients)
    )
    dish.steps.extend(
        DishStep(id=uuid.uuid4(), step_number=number, instruction=text)
        for number, text in enumerate(payload.steps, start=1)
    )

    # Tags saisis : seuls les subjectifs arrivent ici, le schéma refuse les
    # dérivables. Les dérivés sont réécrits juste après par `apply_derived`.
    dish.tags.extend(DishTagLink(tag=tag, is_derived=False) for tag in payload.tags)

    try:
        apply_derived(dish, recompute(dish))
    except UnitConversionError as exc:
        # FN-012 : le refus remonte, il n'est pas absorbé par une valeur nulle.
        raise NutritionError(
            ErrorCode.VALIDATION_ERROR,
            message=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        ) from exc


@admin.get("/dishes", response_model=DishPage, summary="FN-008 — inventaire et file de validation")
async def admin_list_dishes(
    dish_status: DishStatus | None = Query(default=None, alias="status"),
    blocked_only: bool = Query(
        default=False, description="Seulement les plats non publiables"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> DishPage:
    query = _dish_query()
    if dish_status:
        query = query.where(Dish.status == dish_status)
    total = await session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = (
        await session.scalars(query.order_by(Dish.name).limit(limit).offset(offset))
    ).all()
    items = [dish_out(r, with_blockers=True) for r in rows]
    if blocked_only:
        items = [i for i in items if i.publication_blockers]
    return DishPage(total=total or 0, limit=limit, offset=offset, items=items)


@admin.post(
    "/dishes",
    response_model=DishOut,
    status_code=status.HTTP_201_CREATED,
    summary="FN-008 — créer un plat",
)
async def create_dish(
    payload: DishIn,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    if await session.scalar(select(Dish).where(Dish.slug == payload.slug)):
        raise NutritionError(
            ErrorCode.CONFLICT,
            message=f"Un plat porte déjà le slug « {payload.slug} ».",
            http_status=status.HTTP_409_CONFLICT,
        )

    dish = Dish(
        id=uuid.uuid4(),
        slug=payload.slug,
        status=DishStatus.DRAFT,
        author_id=principal.external_user_id,
        meal_types=[m.value for m in payload.meal_types],
    )
    await _apply_dish(session, dish, payload, is_new=True)
    session.add(dish)
    await session.flush()

    await audit.record(
        session, AuditAction.DISH_CREATED,
        external_user_id=principal.external_user_id,
        resource_type="dish", resource_id=dish.id,
        details={"slug": dish.slug},
    )
    return dish_out(dish, with_blockers=True)


@admin.get("/dishes/{key}", response_model=DishOut, summary="FN-008 — lire un plat")
async def admin_read_dish(
    key: str, session: AsyncSession = Depends(get_session)
) -> DishOut:
    return dish_out(await _get_dish(session, key), with_blockers=True)


@admin.put("/dishes/{key}", response_model=DishOut, summary="FN-008 — modifier un plat")
async def update_dish(
    key: str,
    payload: DishIn,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    dish = await _get_dish(session, key)
    if payload.slug != dish.slug:
        raise NutritionError(
            ErrorCode.CONFLICT,
            message="Le slug d'un plat ne se modifie pas (D-12).",
            http_status=status.HTTP_409_CONFLICT,
        )
    if dish.status is DishStatus.ARCHIVED:
        raise NutritionError(
            ErrorCode.CONFLICT,
            message="Un plat archivé ne se modifie plus.",
            http_status=status.HTTP_409_CONFLICT,
        )

    was_published = dish.status is DishStatus.PUBLISHED
    await _apply_dish(session, dish, payload, is_new=False)
    await session.flush()

    if was_published:
        # Modifier un plat publié invalide sa validation : le nutritionniste a
        # signé un contenu, pas un identifiant.
        dish.status = DishStatus.PENDING_VALIDATION
        dish.published_at = None
        dish.validated_by = None
        dish.validated_at = None

    await audit.record(
        session, AuditAction.DISH_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="dish", resource_id=dish.id,
        details={"slug": dish.slug, "revalidation_required": was_published},
    )
    return dish_out(dish, with_blockers=True)


async def _transition(
    session: AsyncSession,
    dish: Dish,
    target: DishStatus,
    principal: Principal,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    if not can_transition(dish.status, target):
        await audit.record(
            session, action,
            external_user_id=principal.external_user_id,
            resource_type="dish", resource_id=dish.id,
            result=AuditResult.DENIED,
            details={"from": dish.status.value, "to": target.value},
        )
        raise NutritionError(
            ErrorCode.CONFLICT,
            message=f"Transition refusée : « {dish.status.value} » → « {target.value} ».",
            http_status=status.HTTP_409_CONFLICT,
        )
    dish.status = target
    await audit.record(
        session, action,
        external_user_id=principal.external_user_id,
        resource_type="dish", resource_id=dish.id,
        details={"slug": dish.slug, **(details or {})},
    )


@admin.post(
    "/dishes/{key}/submit", response_model=DishOut, summary="FN-008 — soumettre à validation"
)
async def submit_dish(
    key: str,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    dish = await _get_dish(session, key)
    await _transition(
        session, dish, DishStatus.PENDING_VALIDATION, principal, AuditAction.DISH_UPDATED
    )
    return dish_out(dish, with_blockers=True)


@admin.post(
    "/dishes/{key}/validate",
    response_model=DishOut,
    summary="FN-008 — valider un plat (nutritionniste)",
    dependencies=[Depends(require_validator)],
)
async def validate_dish(
    key: str,
    principal: Principal = Depends(require_validator),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    dish = await _get_dish(session, key)
    if dish.author_id is not None and dish.author_id == principal.external_user_id:
        # FN-008 : le validateur ne peut pas être l'auteur. La base porte la
        # même contrainte ; la refuser ici produit un message utile.
        raise NutritionError(
            ErrorCode.FORBIDDEN,
            message="Le validateur ne peut pas être l'auteur du plat.",
            http_status=status.HTTP_403_FORBIDDEN,
        )

    await _transition(
        session, dish, DishStatus.VALIDATED, principal, AuditAction.DISH_VALIDATED
    )
    dish.validated_by = principal.external_user_id
    dish.validated_at = datetime.now(UTC)
    dish.rejection_reason = None
    return dish_out(dish, with_blockers=True)


@admin.post(
    "/dishes/{key}/reject",
    response_model=DishOut,
    summary="FN-008 — refuser un plat (nutritionniste)",
    dependencies=[Depends(require_validator)],
)
async def reject_dish(
    key: str,
    payload: RejectIn,
    principal: Principal = Depends(require_validator),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    dish = await _get_dish(session, key)
    await _transition(
        session, dish, DishStatus.REJECTED, principal, AuditAction.DISH_REJECTED
    )
    dish.rejection_reason = payload.reason
    return dish_out(dish, with_blockers=True)


@admin.post(
    "/dishes/{key}/publish",
    response_model=DishOut,
    summary="FN-008 · 🔴 publier un plat",
    dependencies=[Depends(require_validator)],
)
async def publish_dish(
    key: str,
    principal: Principal = Depends(require_validator),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    """Le point de contrôle de FN-003. Un plat dont un ingrédient n'a pas ses
    allergènes signés est refusé, et le motif est renvoyé en clair."""
    dish = await _get_dish(session, key)

    try:
        derived = recompute(dish)
    except UnitConversionError as exc:
        raise NutritionError(
            ErrorCode.VALIDATION_ERROR,
            message=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        ) from exc

    apply_derived(dish, derived)
    blockers = publication_blockers(dish, derived)
    if blockers:
        await audit.record(
            session, AuditAction.DISH_PUBLISHED,
            external_user_id=principal.external_user_id,
            resource_type="dish", resource_id=dish.id,
            result=AuditResult.DENIED,
            details={"slug": dish.slug, "blockers": blockers},
        )
        raise NutritionError(
            ErrorCode.DISH_NOT_PUBLISHABLE,
            message="Publication refusée : " + " · ".join(blockers),
            http_status=status.HTTP_409_CONFLICT,
            recovery_action="verify_ingredients",
        )

    await _transition(
        session, dish, DishStatus.PUBLISHED, principal, AuditAction.DISH_PUBLISHED
    )
    dish.published_at = datetime.now(UTC)
    return dish_out(dish, with_blockers=True)


@admin.post(
    "/dishes/{key}/archive", response_model=DishOut, summary="FN-008 — archiver un plat"
)
async def archive_dish(
    key: str,
    principal: Principal = Depends(require_catalog_editor),
    session: AsyncSession = Depends(get_session),
) -> DishOut:
    """L'archivage remplace la suppression : un plat déjà recommandé ne peut pas
    disparaître, et grâce au figement de FN-023 il n'affecte aucun programme
    passé."""
    dish = await _get_dish(session, key)
    await _transition(
        session, dish, DishStatus.ARCHIVED, principal, AuditAction.DISH_ARCHIVED
    )
    dish.archived_at = datetime.now(UTC)
    dish.published_at = None
    return dish_out(dish, with_blockers=True)
