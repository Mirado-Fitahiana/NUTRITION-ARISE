"""Menu du jour, suivi des repas et feedback (FN-025, FN-031, FN-032).

Ce qui sert l'écran « Menu du jour » du mobile :

* `GET  /meal-plans/today` — la journée du programme actif qui tombe aujourd'hui ;
* `POST /meals/{meal_id}/tracking` — « J'ai suivi ce plat » (ou non) ;
* `POST /meals/{meal_id}/feedback` — avis, dont le signalement d'allergie ;
* `POST /meals/{meal_id}/replace` — « Proposer autre chose ».

**Un repas n'appartient qu'à son utilisateur.** `meal_id` désigne un repas,
jamais une personne : l'appartenance est contrôlée via le programme, et un
repas d'autrui répond `404` comme un repas inexistant.

**Remplacer ne réécrit pas l'histoire** (FN-025). Le remplacement crée une
nouvelle version du programme, où seul le repas visé change ; l'ancienne
version est archivée intacte, et le repas remplacé y garde la trace du refus.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Principal, current_user
from app.core.database import get_session
from app.core.errors import ErrorCode, NutritionError
from app.models.audit import AuditAction
from app.models.catalog import Dish
from app.models.enums import (
    AuditResult,
    DishStatus,
    FeedbackType,
    MealPlanStatus,
    TrackedStatus,
)
from app.models.planning import MealPlan, MealPlanDay, MealPlanMeal, UserMealFeedback
from app.routers.planning import (
    _catalogue_publie,
    _contraintes,
    _plan_complet,
    _plat_candidat,
    _plan_out,
    _poids,
    _regles,
    _snapshot,
)
from app.schemas.planning import (
    DayOut,
    FeedbackIn,
    FeedbackOut,
    GenerateIn,
    TodayOut,
    TrackingIn,
)
from app.services import audit
from app.services.catalog import INCIDENT_REVIEW_TRANSITION
from app.services.recommandation import filtrer, proposer_remplacement

router = APIRouter(prefix="/api/v1/nutrition", tags=["menu du jour"])

logger = logging.getLogger("app.suivi")

#: Feedbacks qui valent refus : le plat n'est plus reproposé sur la période.
REFUS: frozenset[FeedbackType] = frozenset(
    {
        FeedbackType.REJECTED,
        FeedbackType.REPLACED,
        FeedbackType.DISLIKED,
        FeedbackType.ALLERGY_ISSUE,
    }
)


def _introuvable() -> NutritionError:
    return NutritionError(
        ErrorCode.NOT_FOUND,
        message="Repas introuvable.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


async def _repas_de(
    session: AsyncSession, meal_id: uuid.UUID, user_id: str
) -> tuple[MealPlanMeal, MealPlan]:
    """Le repas et son programme, s'ils appartiennent à l'utilisateur."""
    repas = await session.scalar(
        select(MealPlanMeal)
        .where(MealPlanMeal.id == meal_id)
        .options(selectinload(MealPlanMeal.day).selectinload(MealPlanDay.plan))
    )
    if repas is None or repas.day.plan.external_user_id != user_id:
        raise _introuvable()
    plan = repas.day.plan
    if plan.status != MealPlanStatus.ACTIVE:
        # Une version archivée n'est plus affichée : y écrire ferait perdre
        # l'action en silence.
        raise NutritionError(
            ErrorCode.CONFLICT,
            message="Ce programme a été remplacé par une version plus récente.",
            http_status=status.HTTP_409_CONFLICT,
            details={"plan_status": str(plan.status)},
        )
    return repas, plan


async def _programme_du_jour(
    session: AsyncSession, user_id: str, jour: date
) -> MealPlan | None:
    plan_id = await session.scalar(
        select(MealPlan.id)
        .where(
            MealPlan.external_user_id == user_id,
            MealPlan.status == MealPlanStatus.ACTIVE,
            MealPlan.start_date <= jour,
            MealPlan.end_date >= jour,
        )
        .order_by(MealPlan.created_at.desc())
        .limit(1)
    )
    return await _plan_complet(session, plan_id) if plan_id else None


async def _repas_signales(session: AsyncSession, plan: MealPlan) -> frozenset[uuid.UUID]:
    """Repas à ne plus présenter comme une recommandation (FN-032).

    Deux causes, qui se complètent : un signalement d'allergie **sur ce repas**,
    et un plat **en revue** — un plat publié n'y passe que par un incident, et
    ses répétitions dans le programme doivent être signalées aussi, y compris
    dans les copies de version qui ont perdu l'avis de l'ancien repas.
    """
    repas = [m for j in plan.days for m in j.meals]
    if not repas:
        return frozenset()
    avec_avis = set(
        await session.scalars(
            select(UserMealFeedback.meal_id).where(
                UserMealFeedback.meal_id.in_([m.id for m in repas]),
                UserMealFeedback.feedback_type == FeedbackType.ALLERGY_ISSUE,
            )
        )
    )
    en_revue = set(
        await session.scalars(
            select(Dish.id).where(Dish.status == DishStatus.PENDING_VALIDATION)
        )
    )
    return frozenset(
        m.id for m in repas if m.id in avec_avis or (m.dish_id in en_revue)
    )


async def _journee(session: AsyncSession, plan: MealPlan, jour: date) -> TodayOut:
    regles = await _regles(session)
    complet = _plan_out(
        plan, set(regles.meal_distribution), await _repas_signales(session, plan)
    )
    journee: DayOut | None = next(
        (d for d in complet.days if d.day_date == jour), None
    )
    if journee is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message="Votre programme ne couvre pas cette journée.",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    attendus = {str(s) for s in regles.meal_distribution}
    return TodayOut(
        plan_id=plan.id,
        plan_version=plan.version,
        day=journee,
        uncovered_slots=sorted(attendus - {str(m.slot) for m in journee.meals}),
    )


@router.get(
    "/meal-plans/today",
    response_model=TodayOut,
    summary="Menu du jour — la journée du programme actif",
)
async def today(
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TodayOut:
    jour = date.today()
    plan = await _programme_du_jour(session, principal.external_user_id, jour)
    if plan is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message="Aucun programme actif pour aujourd'hui. Générez-en un.",
            http_status=status.HTTP_404_NOT_FOUND,
            details={"recovery_action": "generate_plan"},
        )
    return await _journee(session, plan, jour)


@router.post(
    "/meals/{meal_id}/tracking",
    response_model=TodayOut,
    summary="FN-031 — marquer un repas comme suivi ou non",
)
async def track_meal(
    meal_id: uuid.UUID,
    payload: TrackingIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TodayOut:
    if payload.status in (TrackedStatus.REPLACED,):
        # `replaced` n'est posé que par le remplacement lui-même, qui crée
        # la nouvelle version : l'accepter ici laisserait un repas « remplacé »
        # sans remplaçant.
        raise NutritionError(
            ErrorCode.VALIDATION_ERROR,
            message="Utilisez « Proposer autre chose » pour remplacer un repas.",
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    repas, plan = await _repas_de(session, meal_id, principal.external_user_id)
    repas.tracked_status = payload.status
    repas.consumed_quantity = (
        payload.consumed_quantity
        if payload.status == TrackedStatus.FOLLOWED
        else None
    )
    await audit.record(
        session,
        AuditAction.MEAL_TRACKED,
        external_user_id=principal.external_user_id,
        resource_type="meal_plan_meal",
        resource_id=str(repas.id),
        details={
            "tracked_status": str(payload.status),
            "dish": repas.dish_snapshot.get("slug"),
        },
    )
    await session.commit()
    rafraichi = await _plan_complet(session, plan.id)
    return await _journee(session, rafraichi, repas.day.day_date)


@router.post(
    "/meals/{meal_id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
    summary="FN-032 — donner son avis sur un repas",
)
async def meal_feedback(
    meal_id: uuid.UUID,
    payload: FeedbackIn,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FeedbackOut:
    repas, _plan = await _repas_de(session, meal_id, principal.external_user_id)
    avis = UserMealFeedback(
        id=uuid.uuid4(),
        meal_id=repas.id,
        feedback_type=payload.feedback_type,
        comment=payload.comment,
        created_at=datetime.now(UTC),
    )
    session.add(avis)

    en_revue = False
    if payload.feedback_type == FeedbackType.ALLERGY_ISSUE:
        en_revue = await _incident_allergie(session, repas, principal.external_user_id)
    else:
        await audit.record(
            session,
            AuditAction.MEAL_FEEDBACK,
            external_user_id=principal.external_user_id,
            resource_type="meal_plan_meal",
            resource_id=str(repas.id),
            details={
                "feedback_type": str(payload.feedback_type),
                "dish": repas.dish_snapshot.get("slug"),
            },
        )

    await session.commit()
    return FeedbackOut(
        id=avis.id,
        feedback_type=avis.feedback_type,
        created_at=avis.created_at,
        dish_put_in_review=en_revue,
    )


async def _incident_allergie(
    session: AsyncSession, repas: MealPlanMeal, user_id: str
) -> bool:
    """FN-032 — un signalement d'allergie est un incident, pas un avis.

    Journal en `ERROR` (l'alerte), trace d'audit, et retrait du plat des
    recommandations jusqu'à revalidation par un nutritionniste. Le journal ne
    porte ni l'allergie de l'utilisateur ni son commentaire : seulement le plat.
    """
    slug = repas.dish_snapshot.get("slug")
    logger.error(
        "INCIDENT ALLERGIE — plat %s signalé (repas %s)", slug, repas.id
    )
    await audit.record(
        session,
        AuditAction.ALLERGY_INCIDENT,
        external_user_id=user_id,
        resource_type="meal_plan_meal",
        resource_id=str(repas.id),
        result=AuditResult.FAILURE,
        details={"dish": slug},
    )

    if repas.dish_id is None:
        return False
    plat = await session.get(Dish, repas.dish_id)
    depart, arrivee = INCIDENT_REVIEW_TRANSITION
    if plat is None or plat.status != depart:
        return False
    plat.status = arrivee
    await audit.record(
        session,
        AuditAction.DISH_PUT_IN_REVIEW,
        external_user_id=user_id,
        resource_type="dish",
        resource_id=str(plat.id),
        details={"dish": plat.slug, "motif": "allergy_issue"},
    )
    return True


async def _slugs_refuses(
    session: AsyncSession, user_id: str, plan: MealPlan
) -> set[str]:
    """Plats refusés par l'utilisateur sur la période du programme, toutes
    versions confondues (FN-025 : un plat refusé n'est pas reproposé)."""
    lignes = await session.scalars(
        select(MealPlanMeal.dish_snapshot)
        .join(MealPlanDay, MealPlanMeal.day_id == MealPlanDay.id)
        .join(MealPlan, MealPlanDay.plan_id == MealPlan.id)
        .outerjoin(UserMealFeedback, UserMealFeedback.meal_id == MealPlanMeal.id)
        .where(
            MealPlan.external_user_id == user_id,
            MealPlan.start_date <= plan.end_date,
            MealPlan.end_date >= plan.start_date,
            (MealPlanMeal.tracked_status == TrackedStatus.REPLACED)
            | (UserMealFeedback.feedback_type.in_(list(REFUS))),
        )
    )
    return {s.get("slug") for s in lignes if s and s.get("slug")}


def _copier(plan: MealPlan) -> tuple[MealPlan, dict[uuid.UUID, MealPlanMeal]]:
    """Nouvelle version du programme, identique repas pour repas."""
    copie = MealPlan(
        id=uuid.uuid4(),
        external_user_id=plan.external_user_id,
        profile_id=plan.profile_id,
        start_date=plan.start_date,
        end_date=plan.end_date,
        days_count=plan.days_count,
        household_size=plan.household_size,
        goal=plan.goal,
        kcal_target=plan.kcal_target,
        status=MealPlanStatus.ACTIVE,
        total_cost_min=plan.total_cost_min,
        total_cost_max=plan.total_cost_max,
        cost_confidence=plan.cost_confidence,
        version=plan.version + 1,
        parent_plan_id=plan.id,
    )
    correspondance: dict[uuid.UUID, MealPlanMeal] = {}
    copie.days = []
    for jour in plan.days:
        nouveau_jour = MealPlanDay(
            id=uuid.uuid4(),
            day_index=jour.day_index,
            day_date=jour.day_date,
            kcal_total=jour.kcal_total,
            protein_total=jour.protein_total,
            carbs_total=jour.carbs_total,
            fat_total=jour.fat_total,
            daily_tip=jour.daily_tip,
        )
        nouveau_jour.meals = []
        for repas in jour.meals:
            nouveau = MealPlanMeal(
                id=uuid.uuid4(),
                slot=repas.slot,
                dish_id=repas.dish_id,
                dish_snapshot=dict(repas.dish_snapshot),
                servings=repas.servings,
                kcal=repas.kcal,
                protein_g=repas.protein_g,
                carbs_g=repas.carbs_g,
                fat_g=repas.fat_g,
                estimated_cost=repas.estimated_cost,
                justification=repas.justification,
                tracked_status=repas.tracked_status,
                consumed_quantity=repas.consumed_quantity,
            )
            nouveau_jour.meals.append(nouveau)
            correspondance[repas.id] = nouveau
        copie.days.append(nouveau_jour)
    return copie, correspondance


def _somme(valeurs) -> Decimal | None:
    connues = [Decimal(str(v)) for v in valeurs if v is not None]
    return sum(connues, Decimal("0")) if connues else None


@router.post(
    "/meals/{meal_id}/replace",
    response_model=TodayOut,
    summary="FN-025 — proposer un autre plat pour ce repas",
)
async def replace_meal(
    meal_id: uuid.UUID,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TodayOut:
    user_id = principal.external_user_id
    repas, plan = await _repas_de(session, meal_id, user_id)
    plan = await _plan_complet(session, plan.id)
    jour = next(d for d in plan.days if any(m.id == repas.id for m in d.meals))

    contraintes, _profil = await _contraintes(session, user_id, GenerateIn(goal=plan.goal))
    regles = await _regles(session)
    poids, _version = await _poids(session)
    plats = await _catalogue_publie(session)
    par_slug = {d.slug: d for d in plats}
    vivier = filtrer([_plat_candidat(d) for d in plats], contraintes)

    utilisations: dict[str, int] = {}
    for j in plan.days:
        for m in j.meals:
            slug = m.dish_snapshot.get("slug")
            if slug:
                utilisations[slug] = utilisations.get(slug, 0) + 1

    exclus = await _slugs_refuses(session, user_id, plan)
    exclus |= {m.dish_snapshot.get("slug") for m in jour.meals}
    part = regles.meal_distribution.get(repas.slot, Decimal("0"))
    kcal_cible = (contraintes.kcal_target * part).quantize(Decimal("0.01"))

    choix = proposer_remplacement(
        vivier,
        repas.slot,
        kcal_cible,
        contraintes,
        exclus=frozenset(s for s in exclus if s),
        utilisations=utilisations,
        poids=poids,
    )
    if choix is None:
        raise NutritionError(
            ErrorCode.NO_COMPATIBLE_DISH,
            message="Aucun autre plat compatible n'est disponible pour ce repas.",
            http_status=status.HTTP_409_CONFLICT,
        )

    dish = par_slug[choix.plat.slug]
    # Contrôle de sécurité rejoué sur l'objet en base, pas seulement sur le
    # candidat : FN-022 ne se délègue pas.
    if {str(a.allergen) for a in dish.allergens} & {str(a) for a in contraintes.allergens}:
        raise NutritionError(
            ErrorCode.MEDICAL_LIMIT,
            message="Le remplacement proposé n'a pas passé le contrôle d'allergènes.",
            http_status=status.HTTP_409_CONFLICT,
        )

    copie, correspondance = _copier(plan)
    nouveau = correspondance[repas.id]
    nouveau.dish_id = dish.id
    nouveau.dish_snapshot = _snapshot(dish)
    nouveau.kcal = choix.plat.kcal
    nouveau.protein_g = choix.plat.protein_g
    nouveau.carbs_g = choix.plat.carbs_g
    nouveau.fat_g = choix.plat.fat_g
    nouveau.estimated_cost = choix.plat.estimated_cost
    nouveau.justification = None
    nouveau.tracked_status = TrackedStatus.PENDING
    nouveau.consumed_quantity = None

    for j in copie.days:
        j.kcal_total = _somme(m.kcal for m in j.meals)
    total = _somme(m.estimated_cost for j in copie.days for m in j.meals)
    copie.total_cost_min = copie.total_cost_max = total

    # L'ancienne version reste lisible telle quelle, avec la trace du refus.
    plan.status = MealPlanStatus.ARCHIVED
    repas.tracked_status = TrackedStatus.REPLACED
    session.add(
        UserMealFeedback(
            id=uuid.uuid4(),
            meal_id=repas.id,
            feedback_type=FeedbackType.REPLACED,
            created_at=datetime.now(UTC),
        )
    )
    session.add(copie)
    await audit.record(
        session,
        AuditAction.MEAL_REPLACED,
        external_user_id=user_id,
        resource_type="meal_plan",
        resource_id=str(copie.id),
        details={
            "de": repas.dish_snapshot.get("slug"),
            "vers": dish.slug,
            "version": copie.version,
            "slot": str(repas.slot),
        },
    )
    await session.commit()

    rafraichi = await _plan_complet(session, copie.id)
    return await _journee(session, rafraichi, jour.day_date)


__all__ = ["router"]
