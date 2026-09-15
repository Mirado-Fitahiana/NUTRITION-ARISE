"""Profil nutritionnel, préférences, allergies et restrictions (FN-001 → FN-004).

Une règle traverse tout le fichier : **le profil est toujours résolu depuis le
claim `sub` du jeton**, jamais depuis un identifiant fourni par le client. Il
n'existe donc pas de route « profil de quelqu'un d'autre » à sécuriser — elle
n'existe pas du tout. C'est le critère d'acceptation le plus important de
FN-001, et le test §14.5 qui va avec.

Deuxième règle : une modification du profil **rend obsolètes** les programmes
actifs sans les supprimer (FN-001). L'historique doit rester consultable tel
qu'il a été délivré ; c'est le pendant du figement de FN-023.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Principal, current_user
from app.core.database import get_session
from app.core.errors import ErrorCode, NutritionError
from app.models.catalog import Ingredient
from app.models.enums import AuditResult, IngredientStatus, MealPlanStatus
from app.models.planning import MealPlan
from app.models.profile import (
    DietaryPreference,
    DietaryRestriction,
    DislikedIngredient,
    FavoriteIngredient,
    NutritionProfile,
    NutritionProfileHistory,
    UserAllergy,
)
from app.schemas.profile import (
    AllergiesIn,
    AllergyOut,
    PreferencesIn,
    PreferencesOut,
    ProfileHistoryOut,
    ProfileIn,
    ProfileOut,
    RestrictionOut,
    RestrictionsIn,
    RestrictionsOut,
)
from app.services import audit
from app.services.audit import AuditAction
from app.services.restrictions import IngredientIndex, normalize

router = APIRouter(prefix="/api/v1/nutrition", tags=["profil"])

#: Champs du profil suivis dans l'historique (FN-001).
TRACKED_FIELDS = (
    "goal", "weight_kg", "height_cm", "birth_date", "sex", "activity_level",
    "meals_per_day", "household_size", "daily_budget", "currency", "city",
    "district", "travel_radius_km", "declared_pregnancy",
    "declared_breastfeeding", "declared_medical_condition",
)

#: Champs dont la modification invalide les besoins calculés, donc les
#: programmes actifs (FN-001, FN-038).
NUTRITION_RELEVANT = frozenset(
    {"weight_kg", "height_cm", "birth_date", "sex", "activity_level", "goal",
     "meals_per_day"}
)


# --------------------------------------------------------------------------
# Accès au profil
# --------------------------------------------------------------------------


async def _load(session: AsyncSession, user_id: str) -> NutritionProfile | None:
    return await session.scalar(
        select(NutritionProfile)
        .where(NutritionProfile.external_user_id == user_id)
        .options(
            selectinload(NutritionProfile.allergies),
            selectinload(NutritionProfile.restrictions),
            selectinload(NutritionProfile.preferences),
        )
    )


async def _require(session: AsyncSession, user_id: str) -> NutritionProfile:
    profile = await _load(session, user_id)
    if profile is None:
        raise NutritionError(
            ErrorCode.PROFILE_INCOMPLETE,
            message="Aucun profil nutritionnel n'existe pour ce compte.",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return profile


def _age(birth_date: date, on: date | None = None) -> int:
    today = on or date.today()
    return today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )


def _bmi(weight_kg: Decimal, height_cm: Decimal) -> Decimal | None:
    if not height_cm:
        return None
    metres = Decimal(height_cm) / Decimal("100")
    return (Decimal(weight_kg) / (metres * metres)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )


def _to_out(profile: NutritionProfile) -> ProfileOut:
    out = ProfileOut.model_validate(profile)
    out.age = _age(profile.birth_date)
    out.bmi = _bmi(profile.weight_kg, profile.height_cm)
    return out


async def _mark_plans_obsolete(session: AsyncSession, profile_id) -> int:
    """FN-001 — les programmes actifs deviennent obsolètes, jamais supprimés."""
    plans = (
        await session.scalars(
            select(MealPlan).where(
                MealPlan.profile_id == profile_id,
                MealPlan.status == MealPlanStatus.ACTIVE,
            )
        )
    ).all()
    for plan in plans:
        plan.status = MealPlanStatus.OBSOLETE
    return len(plans)


# --------------------------------------------------------------------------
# FN-001 — profil
# --------------------------------------------------------------------------


@router.get("/profile", response_model=ProfileOut, summary="FN-001 — lire son profil")
async def read_profile(
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProfileOut:
    return _to_out(await _require(session, principal.external_user_id))


@router.post(
    "/profile",
    response_model=ProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="FN-001 — créer son profil",
)
async def create_profile(
    payload: ProfileIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProfileOut:
    if await _load(session, principal.external_user_id) is not None:
        raise NutritionError(
            ErrorCode.CONFLICT,
            message="Un profil existe déjà pour ce compte ; utiliser PUT pour le modifier.",
            http_status=status.HTTP_409_CONFLICT,
        )

    profile = NutritionProfile(external_user_id=principal.external_user_id)
    data = payload.model_dump(exclude={"disclaimer_accepted"})
    for field, value in data.items():
        setattr(profile, field, value)
    if payload.disclaimer_accepted:
        profile.disclaimer_accepted_at = datetime.now(UTC)

    session.add(profile)
    await session.flush()

    await audit.record(
        session,
        AuditAction.PROFILE_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="nutrition_profile",
        resource_id=profile.id,
        details={"operation": "create", "goal": payload.goal.value},
    )
    return _to_out(profile)


@router.put("/profile", response_model=ProfileOut, summary="FN-001 — modifier son profil")
async def update_profile(
    payload: ProfileIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProfileOut:
    profile = await _require(session, principal.external_user_id)

    previous: dict[str, Any] = {}
    changed: list[str] = []
    for field in TRACKED_FIELDS:
        new = getattr(payload, field)
        old = getattr(profile, field)
        if str(old) != str(new):
            previous[field] = str(old)
            changed.append(field)
            setattr(profile, field, new)

    # L'accusé de lecture ne se retire pas : l'avertissement a été vu.
    if payload.disclaimer_accepted and profile.disclaimer_accepted_at is None:
        profile.disclaimer_accepted_at = datetime.now(UTC)
        changed.append("disclaimer_accepted_at")

    obsolete = 0
    if changed:
        session.add(
            NutritionProfileHistory(
                profile_id=profile.id,
                changed_at=datetime.now(UTC),
                changed_fields=changed,
                previous_values=previous,
                correlation_id=None,
            )
        )
        if NUTRITION_RELEVANT & set(changed):
            obsolete = await _mark_plans_obsolete(session, profile.id)

        await audit.record(
            session,
            AuditAction.PROFILE_UPDATED,
            external_user_id=principal.external_user_id,
            resource_type="nutrition_profile",
            resource_id=profile.id,
            # Les noms des champs modifiés, jamais leurs valeurs (FN-036).
            details={"changed_fields": changed, "plans_marked_obsolete": obsolete},
        )

    return _to_out(profile)


@router.get(
    "/profile/history",
    response_model=list[ProfileHistoryOut],
    summary="FN-001 — historique des modifications",
)
async def profile_history(
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProfileHistoryOut]:
    profile = await _require(session, principal.external_user_id)
    rows = (
        await session.scalars(
            select(NutritionProfileHistory)
            .where(NutritionProfileHistory.profile_id == profile.id)
            .order_by(NutritionProfileHistory.changed_at.desc())
            .limit(100)
        )
    ).all()
    return [ProfileHistoryOut.model_validate(r) for r in rows]


# --------------------------------------------------------------------------
# FN-002 — préférences
# --------------------------------------------------------------------------


async def _resolve_slugs(
    session: AsyncSession, slugs: list[str]
) -> dict[str, Ingredient]:
    if not slugs:
        return {}
    rows = (
        await session.scalars(
            select(Ingredient).where(Ingredient.slug.in_(sorted(set(slugs))))
        )
    ).all()
    found = {row.slug: row for row in rows}
    missing = sorted(set(slugs) - set(found))
    if missing:
        raise NutritionError(
            ErrorCode.VALIDATION_ERROR,
            message="Ingrédient(s) inconnu(s) du catalogue : " + ", ".join(missing),
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return found


async def _preferences_out(
    session: AsyncSession, profile: NutritionProfile
) -> PreferencesOut:
    prefs = profile.preferences
    out = (
        PreferencesOut.model_validate(prefs)
        if prefs is not None
        else PreferencesOut(
            prefer_local_products=False, prefer_quick_dishes=False,
            prefer_easy_dishes=False, prefer_economical_dishes=False,
            vegetarian_preference=False, preferred_cuisines=[],
            preferred_protein_types=[], max_food_frequency=None,
        )
    )
    for table, target in (
        (FavoriteIngredient, "favorite_ingredients"),
        (DislikedIngredient, "disliked_ingredients"),
    ):
        slugs = (
            await session.scalars(
                select(Ingredient.slug)
                .join(table, table.ingredient_id == Ingredient.id)
                .where(table.profile_id == profile.id)
                .order_by(Ingredient.slug)
            )
        ).all()
        setattr(out, target, list(slugs))
    return out


@router.get(
    "/preferences", response_model=PreferencesOut, summary="FN-002 — lire ses préférences"
)
async def read_preferences(
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PreferencesOut:
    profile = await _require(session, principal.external_user_id)
    return await _preferences_out(session, profile)


@router.put(
    "/preferences", response_model=PreferencesOut, summary="FN-002 — définir ses préférences"
)
async def write_preferences(
    payload: PreferencesIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PreferencesOut:
    profile = await _require(session, principal.external_user_id)
    ingredients = await _resolve_slugs(
        session, payload.favorite_ingredients + payload.disliked_ingredients
    )

    prefs = profile.preferences
    if prefs is None:
        prefs = DietaryPreference(profile_id=profile.id)
        session.add(prefs)
        profile.preferences = prefs

    for field in (
        "prefer_local_products", "prefer_quick_dishes", "prefer_easy_dishes",
        "prefer_economical_dishes", "vegetarian_preference", "preferred_cuisines",
        "preferred_protein_types", "max_food_frequency",
    ):
        setattr(prefs, field, getattr(payload, field))

    for table, slugs in (
        (FavoriteIngredient, payload.favorite_ingredients),
        (DislikedIngredient, payload.disliked_ingredients),
    ):
        for row in (
            await session.scalars(select(table).where(table.profile_id == profile.id))
        ).all():
            await session.delete(row)
        await session.flush()
        for slug in dict.fromkeys(slugs):
            session.add(
                table(
                    profile_id=profile.id,
                    ingredient_id=ingredients[slug].id,
                    created_at=datetime.now(UTC),
                )
            )
    await session.flush()

    await audit.record(
        session,
        AuditAction.PROFILE_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="dietary_preferences",
        resource_id=profile.id,
        details={
            "favorites": len(payload.favorite_ingredients),
            # Un aliment refusé est bloquant : le compte est utile au débogage
            # d'un pool vide (FN-019).
            "disliked": len(payload.disliked_ingredients),
        },
    )
    return await _preferences_out(session, profile)


# --------------------------------------------------------------------------
# FN-003 — allergies
# --------------------------------------------------------------------------


@router.get(
    "/allergies", response_model=list[AllergyOut], summary="FN-003 — lire ses allergies"
)
async def read_allergies(
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AllergyOut]:
    profile = await _require(session, principal.external_user_id)
    return [
        AllergyOut.model_validate(a)
        for a in sorted(profile.allergies, key=lambda a: a.allergen.value)
    ]


@router.put(
    "/allergies",
    response_model=list[AllergyOut],
    summary="FN-003 — déclarer ses allergies (remplacement intégral)",
)
async def write_allergies(
    payload: AllergiesIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AllergyOut]:
    """Toute modification est journalisée : FN-036 cite explicitement la
    modification d'allergie parmi les événements à tracer, et c'est ce journal
    qui permettra de reconstituer une chronologie en cas d'incident."""
    profile = await _require(session, principal.external_user_id)

    before = {a.allergen for a in profile.allergies}
    after = set(payload.allergens)

    for row in list(profile.allergies):
        if row.allergen not in after:
            await session.delete(row)
    await session.flush()

    now = datetime.now(UTC)
    for allergen in payload.allergens:
        if allergen not in before:
            session.add(
                UserAllergy(
                    profile_id=profile.id, allergen=allergen, declared_at=now
                )
            )
    await session.flush()

    if before != after:
        await _mark_plans_obsolete(session, profile.id)
        await audit.record(
            session,
            AuditAction.ALLERGY_CHANGED,
            external_user_id=principal.external_user_id,
            resource_type="user_allergies",
            resource_id=profile.id,
            # Les allergènes eux-mêmes sont des données de santé : seul le
            # nombre est journalisé (FN-036).
            details={"added": len(after - before), "removed": len(before - after)},
        )

    rows = (
        await session.scalars(
            select(UserAllergy)
            .where(UserAllergy.profile_id == profile.id)
            .order_by(UserAllergy.allergen)
        )
    ).all()
    return [AllergyOut.model_validate(r) for r in rows]


# --------------------------------------------------------------------------
# FN-004 — restrictions
# --------------------------------------------------------------------------


async def _index(session: AsyncSession) -> IngredientIndex:
    rows = (
        await session.execute(
            select(Ingredient.id, Ingredient.name, Ingredient.slug).where(
                Ingredient.status == IngredientStatus.ACTIVE
            )
        )
    ).all()
    aliases: dict = {}
    for ingredient in (
        await session.scalars(
            select(Ingredient).options(selectinload(Ingredient.aliases))
        )
    ).all():
        aliases[ingredient.id] = [a.alias for a in ingredient.aliases]
    return IngredientIndex.build(
        [(r[0], r[1], r[2], aliases.get(r[0], [])) for r in rows]
    )


async def _restrictions_out(
    session: AsyncSession, profile: NutritionProfile
) -> RestrictionsOut:
    rows = (
        await session.scalars(
            select(DietaryRestriction)
            .where(DietaryRestriction.profile_id == profile.id)
            .order_by(DietaryRestriction.created_at)
        )
    ).all()

    slug_by_id: dict = {}
    ids = {i for row in rows for i in row.normalized_ingredient_ids}
    if ids:
        for ingredient_id, slug in (
            await session.execute(
                select(Ingredient.id, Ingredient.slug).where(Ingredient.id.in_(ids))
            )
        ).all():
            slug_by_id[ingredient_id] = slug

    items: list[RestrictionOut] = []
    for row in rows:
        out = RestrictionOut.model_validate(row)
        out.normalized_ingredients = [
            slug_by_id[i] for i in row.normalized_ingredient_ids if i in slug_by_id
        ]
        items.append(out)

    return RestrictionsOut(
        restrictions=items,
        pending_review=[
            r.custom_label or r.restriction_type.value
            for r in rows
            if r.needs_admin_review
        ],
    )


@router.get(
    "/restrictions", response_model=RestrictionsOut, summary="FN-004 — lire ses restrictions"
)
async def read_restrictions(
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RestrictionsOut:
    profile = await _require(session, principal.external_user_id)
    return await _restrictions_out(session, profile)


@router.put(
    "/restrictions",
    response_model=RestrictionsOut,
    summary="FN-004 — déclarer ses restrictions (remplacement intégral)",
)
async def write_restrictions(
    payload: RestrictionsIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RestrictionsOut:
    profile = await _require(session, principal.external_user_id)
    index = await _index(session)

    for row in (
        await session.scalars(
            select(DietaryRestriction).where(
                DietaryRestriction.profile_id == profile.id
            )
        )
    ).all():
        await session.delete(row)
    await session.flush()

    now = datetime.now(UTC)
    unmatched_total = 0
    for entry in payload.restrictions:
        result = normalize(entry.restriction_type, entry.custom_label, index)
        unmatched_total += len(result.unmatched)
        session.add(
            DietaryRestriction(
                profile_id=profile.id,
                restriction_type=entry.restriction_type,
                custom_label=entry.custom_label,
                normalized_ingredient_ids=list(result.ingredient_ids),
                normalized_tags=list(result.tags),
                is_normalized=result.is_normalized,
                needs_admin_review=result.needs_admin_review,
                created_at=now,
            )
        )
    await session.flush()
    await _mark_plans_obsolete(session, profile.id)

    await audit.record(
        session,
        AuditAction.PROFILE_UPDATED,
        external_user_id=principal.external_user_id,
        resource_type="dietary_restrictions",
        resource_id=profile.id,
        details={
            "count": len(payload.restrictions),
            "unmatched_terms": unmatched_total,
        },
    )
    return await _restrictions_out(session, profile)


# --------------------------------------------------------------------------
# §4.6 — export des données
# --------------------------------------------------------------------------


@router.post("/export", summary="§4.6 — export de ses données nutritionnelles")
async def export_data(
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Export JSON, à la demande de l'utilisateur. Ne contient que ses propres
    données : la requête ne prend aucun identifiant en entrée."""
    profile = await _require(session, principal.external_user_id)

    await audit.record(
        session,
        "profile.exported",
        external_user_id=principal.external_user_id,
        resource_type="nutrition_profile",
        resource_id=profile.id,
        result=AuditResult.SUCCESS,
    )

    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "profile": _to_out(profile).model_dump(mode="json"),
        "preferences": (await _preferences_out(session, profile)).model_dump(mode="json"),
        "allergies": [
            AllergyOut.model_validate(a).model_dump(mode="json")
            for a in sorted(profile.allergies, key=lambda a: a.allergen.value)
        ],
        "restrictions": (await _restrictions_out(session, profile)).model_dump(
            mode="json"
        ),
    }
