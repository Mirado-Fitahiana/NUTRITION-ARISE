"""Génération de programme alimentaire (FN-023, sprint 09).

**Asynchrone avec sondage.** La chaîne filtrage → score → composition →
validation → rédaction dépasse régulièrement quelques secondes, la table
`generation_jobs` existe pour ça, et le mobile sait déjà sonder : c'est le
mécanisme du paiement MVola. Aucun schéma nouveau à apprendre côté client.

**Tâches de fond natives, pas Celery.** À ce volume, une file distribuée serait
de l'infrastructure sans besoin. Le jour où le volume l'impose, seul le mode
d'exécution change — le contrat d'API reste identique.

**Aucune route n'accepte d'identifiant utilisateur.** L'utilisateur vient du
claim `sub`. `tests/test_isolation.py` le vérifie par introspection sur toute
route de ce préfixe, y compris celles ajoutées ici.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Principal, current_user
from app.core.database import SessionFactory, get_session
from app.core.errors import ErrorCode, NutritionError
from app.models.catalog import Dish, DishIngredient, Ingredient
from app.models.configuration import AppSetting, ScoringWeightSet
from app.models.enums import (
    Allergen,
    CostConfidence,
    DishStatus,
    Goal,
    JobStatus,
    MealPlanStatus,
    MealSlot,
    RestrictionType,
    ValidationOutcome,
)
from app.models.planning import (
    GenerationJob,
    MealPlan,
    MealPlanDay,
    MealPlanMeal,
    PlanValidation,
    RecommendationCandidate,
    RecommendationRun,
)
from app.models.profile import NutritionProfile, NutritionTarget
from app.schemas.planning import (
    DayOut,
    GenerateIn,
    JobAccepted,
    JobOut,
    MealOut,
    PlanOut,
    PlanPage,
    PlanSummary,
)
from app.services import audit, reglages
from app.services.audit import AuditAction
from app.services.recommandation import (
    POIDS_PAR_DEFAUT,
    REPARTITION_PAR_DEFAUT,
    ContraintesUtilisateur,
    EchecGeneration,
    PlatCandidat,
    ProgrammeGenere,
    ReglesComposition,
    composer,
    filtrer,
    justification_de_repli,
    valider,
)

router = APIRouter(prefix="/api/v1/nutrition", tags=["programmes"])

#: Un utilisateur ne doit pas pouvoir lancer dix générations de front : chacune
#: mobilise le catalogue entier, et rien n'en sort de plus utile.
MAX_JOBS_EN_COURS = 2

SCORING_VERSION_DEFAUT = "fallback-1"


# --------------------------------------------------------------------------
# Chargement de la configuration
# --------------------------------------------------------------------------


async def _regles(session: AsyncSession) -> ReglesComposition:
    """Lit `app_settings` à travers le registre typé (`app.services.reglages`).

    Les valeurs codées en dur ne sont qu'un repli. Une valeur stockée hors
    bornes ou illisible retombe sur le défaut, comme avant — mais elle n'est
    plus ignorée en silence : `/pilotage/reglages` la signale, avec son motif.
    """
    return reglages.regles_depuis(await reglages.lire_etats(session))


async def _poids(session: AsyncSession) -> tuple[dict[str, Decimal], str]:
    """FN-020 — poids **lus en base**, et version tracée dans le run.

    Seules les clés que `scorer` lit sont retenues : une clé inconnue ou une
    valeur hors bornes est ignorée et journalisée (`reglages.poids_depuis`).
    """
    actif = await session.scalar(
        select(ScoringWeightSet).where(ScoringWeightSet.is_active.is_(True))
    )
    if actif is None:
        return dict(POIDS_PAR_DEFAUT), SCORING_VERSION_DEFAUT

    poids, _ignores = reglages.poids_depuis(actif.weights)
    return poids, actif.version


# --------------------------------------------------------------------------
# Chargement du catalogue et du profil
# --------------------------------------------------------------------------


def _plat_candidat(dish: Dish) -> PlatCandidat:
    return PlatCandidat(
        slug=dish.slug,
        name=dish.name,
        meal_types=frozenset(dish.meal_types or ()),
        kcal=Decimal(str(dish.kcal_portion or 0)),
        protein_g=Decimal(str(dish.protein_portion or 0)),
        carbs_g=Decimal(str(dish.carbs_portion or 0)),
        fat_g=Decimal(str(dish.fat_portion or 0)),
        allergens=frozenset(Allergen(a.allergen) for a in dish.allergens),
        compatible_restrictions=frozenset(
            RestrictionType(r) for r in (dish.compatible_restrictions or ())
        ),
        compatible_goals=frozenset(Goal(g) for g in (dish.compatible_goals or ())),
        ingredient_slugs=frozenset(c.ingredient.slug for c in dish.ingredients),
        protein_sources=frozenset(
            c.ingredient.slug
            for c in dish.ingredients
            if str(c.ingredient.category) in {"meats", "fish", "legumes", "dairy"}
        ),
        estimated_cost=(
            Decimal(str(dish.estimated_cost)) if dish.estimated_cost is not None else None
        ),
    )


async def _catalogue_publie(session: AsyncSession) -> list[Dish]:
    """FN-008 — seul un plat publié peut être recommandé."""
    return list(
        await session.scalars(
            select(Dish)
            .where(Dish.status == DishStatus.PUBLISHED)
            .options(
                selectinload(Dish.ingredients).selectinload(DishIngredient.ingredient),
                selectinload(Dish.allergens),
            )
        )
    )


async def _contraintes(
    session: AsyncSession, user_id: str, demande: GenerateIn
) -> tuple[ContraintesUtilisateur, NutritionProfile]:
    profil = await session.scalar(
        select(NutritionProfile)
        .where(NutritionProfile.external_user_id == user_id)
        .options(
            selectinload(NutritionProfile.allergies),
            selectinload(NutritionProfile.restrictions),
            selectinload(NutritionProfile.preferences),
        )
    )
    if profil is None:
        raise NutritionError(
            ErrorCode.PROFILE_INCOMPLETE,
            message="Renseignez votre profil nutritionnel avant de générer un programme.",
            http_status=status.HTTP_409_CONFLICT,
        )

    cible = await session.scalar(
        select(NutritionTarget)
        .where(NutritionTarget.profile_id == profil.id)
        .order_by(NutritionTarget.id.desc())
        .limit(1)
    )
    if cible is None:
        raise NutritionError(
            ErrorCode.PROFILE_INCOMPLETE,
            message="Vos besoins énergétiques n'ont pas encore été calculés.",
            http_status=status.HTTP_409_CONFLICT,
        )

    return (
        ContraintesUtilisateur(
            goal=demande.goal,
            kcal_target=Decimal(str(cible.kcal_target)),
            allergens=frozenset(Allergen(a.allergen) for a in profil.allergies),
            restrictions=frozenset(
                RestrictionType(r.restriction_type) for r in profil.restrictions
            ),
            budget_par_jour=demande.budget_mga,
        ),
        profil,
    )


# --------------------------------------------------------------------------
# Exécution
# --------------------------------------------------------------------------


def _snapshot(dish: Dish) -> dict:
    """§4 — copie figée. Un plat archivé ou modifié plus tard ne doit jamais
    altérer un programme déjà délivré."""
    return {
        "slug": dish.slug,
        "name": dish.name,
        "description": dish.description,
        "meal_types": list(dish.meal_types or ()),
        "servings": dish.servings,
        "kcal_portion": str(dish.kcal_portion or 0),
        "protein_portion": str(dish.protein_portion or 0),
        "carbs_portion": str(dish.carbs_portion or 0),
        "fat_portion": str(dish.fat_portion or 0),
        "estimated_cost": (
            str(dish.estimated_cost) if dish.estimated_cost is not None else None
        ),
        "allergens": sorted(str(a.allergen) for a in dish.allergens),
        "ingredients": [
            {
                "slug": c.ingredient.slug,
                "name": c.ingredient.name,
                "quantity": str(c.quantity),
                "unit": c.unit,
            }
            for c in dish.ingredients
        ],
        "frozen_at": datetime.now(UTC).isoformat(),
    }


async def _executer_generation(job_id: uuid.UUID, demande: GenerateIn, user_id: str) -> None:
    """Tâche de fond. Ouvre **sa propre session** : celle de la requête est
    fermée dès la réponse 202 rendue."""
    async with SessionFactory() as session:
        job = await session.get(GenerationJob, job_id)
        if job is None:
            return

        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        await session.commit()

        try:
            contraintes, profil = await _contraintes(session, user_id, demande)
            regles = await _regles(session)
            poids, version_poids = await _poids(session)

            plats = await _catalogue_publie(session)
            par_slug = {d.slug: d for d in plats}
            vivier = filtrer([_plat_candidat(d) for d in plats], contraintes)

            resultat = composer(
                vivier, contraintes, demande.days, regles, poids=poids, seed=demande.seed
            )
            if isinstance(resultat, EchecGeneration):
                job.status = JobStatus.FAILED
                job.error_code = resultat.code
                job.error_detail = resultat.message
                job.finished_at = datetime.now(UTC)
                await session.commit()
                return

            plan = await _materialiser(
                session,
                resultat,
                contraintes,
                regles,
                profil,
                par_slug,
                user_id=user_id,
                version_poids=version_poids,
            )

            job.plan_id = plan.id
            job.status = JobStatus.SUCCEEDED
            job.finished_at = datetime.now(UTC)
            await session.commit()

        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            job = await session.get(GenerationJob, job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.error_code = (
                    exc.code.value if isinstance(exc, NutritionError) else ErrorCode.INTERNAL_ERROR.value
                )
                job.error_detail = str(exc)[:500]
                job.finished_at = datetime.now(UTC)
                await session.commit()


async def _materialiser(
    session: AsyncSession,
    programme: ProgrammeGenere,
    contraintes: ContraintesUtilisateur,
    regles: ReglesComposition,
    profil: NutritionProfile,
    par_slug: dict[str, Dish],
    *,
    user_id: str,
    version_poids: str,
) -> MealPlan:
    debut = date.today()
    plan = MealPlan(
        id=uuid.uuid4(),
        external_user_id=user_id,
        profile_id=profil.id,
        start_date=debut,
        end_date=debut + timedelta(days=len(programme.journees) - 1),
        days_count=len(programme.journees),
        goal=contraintes.goal,
        kcal_target=contraintes.kcal_target,
        status=MealPlanStatus.GENERATING,
        cost_confidence=CostConfidence.ESTIMATED,
    )
    session.add(plan)
    await session.flush()

    run = RecommendationRun(
        id=uuid.uuid4(),
        plan_id=plan.id,
        scoring_version=version_poids,
        random_seed=programme.seed,
        # D-07 — le repli textuel ne consomme aucun appel. La contrainte
        # `llm_call_count <= 1` rend la dérive impossible, pas seulement
        # improbable.
        llm_call_count=0,
        used_fallback=True,
        created_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()

    cout_min = cout_max = Decimal("0")
    for journee in programme.journees:
        jour = MealPlanDay(
            id=uuid.uuid4(),
            plan_id=plan.id,
            day_index=journee.index,
            day_date=debut + timedelta(days=journee.index),
            kcal_total=journee.kcal_total,
        )
        session.add(jour)
        await session.flush()

        for rang, repas in enumerate(journee.repas):
            dish = par_slug[repas.plat.slug]
            session.add(
                MealPlanMeal(
                    id=uuid.uuid4(),
                    day_id=jour.id,
                    slot=repas.slot,
                    dish_id=dish.id,
                    dish_snapshot=_snapshot(dish),
                    servings=1,
                    kcal=repas.plat.kcal,
                    protein_g=repas.plat.protein_g,
                    carbs_g=repas.plat.carbs_g,
                    fat_g=repas.plat.fat_g,
                    estimated_cost=repas.plat.estimated_cost,
                    justification=None,
                )
            )
            session.add(
                RecommendationCandidate(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    day_index=journee.index,
                    slot=repas.slot,
                    dish_id=dish.id,
                    score=repas.score.total,
                    score_breakdown={k: str(v) for k, v in repas.score.detail.items()},
                    rank=rang,
                    selected=True,
                )
            )
            if repas.plat.estimated_cost is not None:
                cout_min += repas.plat.estimated_cost
                cout_max += repas.plat.estimated_cost

    # FN-022 — validation après sélection. Rejouée après le LLM le jour où il
    # interviendra : le texte arrive après, rien ne garantit qu'il n'ait pas
    # déformé la sélection.
    verdict = valider(
        programme, contraintes, regles, catalogue_autorise=frozenset(par_slug)
    )
    session.add(
        PlanValidation(
            id=uuid.uuid4(),
            plan_id=plan.id,
            phase="post_selection",
            outcome=(
                ValidationOutcome.VALIDATED
                if verdict.conforme
                else ValidationOutcome.REJECTED
            ),
            failed_checks=list(verdict.controles_en_echec),
            corrections={},
            is_food_safety_incident=verdict.incident_securite,
            created_at=datetime.now(UTC),
        )
    )

    if verdict.incident_securite:
        # Un incident de sécurité alimentaire ne se corrige pas en aval : le
        # programme n'est pas délivré.
        plan.status = MealPlanStatus.FAILED
        await audit.record(
            session,
            AuditAction.PLAN_VALIDATION_REJECTED,
            external_user_id=user_id,
            resource_type="meal_plan",
            resource_id=plan.id,
            details={"controles": list(verdict.controles_en_echec)},
        )
        await session.commit()
        raise NutritionError(
            ErrorCode.MEDICAL_LIMIT,
            message="Le programme généré n'a pas passé le contrôle de sécurité alimentaire.",
            http_status=status.HTTP_409_CONFLICT,
        )

    plan.status = MealPlanStatus.ACTIVE
    plan.total_cost_min = cout_min or None
    plan.total_cost_max = cout_max or None

    texte = justification_de_repli(programme, contraintes)
    for jour in await session.scalars(
        select(MealPlanDay).where(MealPlanDay.plan_id == plan.id)
    ):
        jour.daily_tip = texte

    await audit.record(
        session,
        AuditAction.PLAN_GENERATED,
        external_user_id=user_id,
        resource_type="meal_plan",
        resource_id=plan.id,
        details={
            "jours": plan.days_count,
            "vivier": programme.vivier_taille,
            "seed": programme.seed,
            "scoring_version": version_poids,
        },
    )
    await session.commit()
    return plan


# --------------------------------------------------------------------------
# Sérialisation
# --------------------------------------------------------------------------


def _plan_out(plan: MealPlan, creneaux_attendus: set[MealSlot]) -> PlanOut:
    # Les créneaux qu'aucun plat n'a pu couvrir sont **déduits à la lecture** :
    # un catalogue sans petit-déjeuner produit un programme sans petit-déjeuner,
    # et le client doit le voir plutôt que de croire à un oubli d'affichage.
    manquants = sorted(
        {
            str(slot)
            for jour in plan.days
            for slot in creneaux_attendus - {repas.slot for repas in jour.meals}
        }
    )
    return PlanOut(
        uncovered_slots=manquants,
        id=plan.id,
        status=plan.status,
        goal=plan.goal,
        start_date=plan.start_date,
        end_date=plan.end_date,
        days_count=plan.days_count,
        kcal_target=plan.kcal_target,
        total_cost_min=plan.total_cost_min,
        total_cost_max=plan.total_cost_max,
        cost_confidence=str(plan.cost_confidence),
        version=plan.version,
        days=[
            DayOut(
                day_index=jour.day_index,
                day_date=jour.day_date,
                kcal_total=jour.kcal_total,
                meals=[
                    MealOut(
                        slot=repas.slot,
                        dish_slug=repas.dish_snapshot.get("slug"),
                        # Le nom vient du **snapshot**, pas du catalogue : c'est
                        # tout l'objet du figeage.
                        name=repas.dish_snapshot.get("name", "—"),
                        kcal=repas.kcal,
                        protein_g=repas.protein_g,
                        carbs_g=repas.carbs_g,
                        fat_g=repas.fat_g,
                        estimated_cost=repas.estimated_cost,
                        justification=repas.justification or jour.daily_tip,
                    )
                    for repas in sorted(jour.meals, key=lambda m: str(m.slot))
                ],
            )
            for jour in plan.days
        ],
    )


async def _plan_complet(session: AsyncSession, plan_id: uuid.UUID) -> MealPlan | None:
    return await session.scalar(
        select(MealPlan)
        .where(MealPlan.id == plan_id)
        .options(selectinload(MealPlan.days).selectinload(MealPlanDay.meals))
    )


# --------------------------------------------------------------------------
# Points d'entrée
# --------------------------------------------------------------------------


@router.post(
    "/meal-plans/generate",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="FN-023 — lancer une génération (asynchrone)",
)
async def generate(
    payload: GenerateIn,
    background: BackgroundTasks,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobAccepted:
    """202 Accepted, puis sondage toutes les 3 à 5 secondes.

    Le profil et les cibles sont vérifiés **avant** d'accepter : rendre un
    `job_id` pour un travail voué à échouer ferait payer au client un aller-
    retour de sondage pour apprendre ce qu'on savait déjà.
    """
    await _contraintes(session, principal.external_user_id, payload)

    en_cours = await session.scalar(
        select(func.count())
        .select_from(GenerationJob)
        .where(
            GenerationJob.external_user_id == principal.external_user_id,
            GenerationJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
        )
    )
    if (en_cours or 0) >= MAX_JOBS_EN_COURS:
        raise NutritionError(
            ErrorCode.QUOTA_EXCEEDED,
            message=(
                f"{MAX_JOBS_EN_COURS} générations sont déjà en cours. "
                "Attendez qu'elles se terminent."
            ),
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    job = GenerationJob(
        id=uuid.uuid4(),
        external_user_id=principal.external_user_id,
        status=JobStatus.PENDING,
        request_params=payload.model_dump(mode="json"),
        created_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()

    background.add_task(
        _executer_generation, job.id, payload, principal.external_user_id
    )
    return JobAccepted(job_id=job.id, status=job.status)


@router.get(
    "/meal-plans/{job_id}",
    response_model=JobOut,
    summary="FN-023 — suivre une génération",
)
async def job_status(
    job_id: uuid.UUID,
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    """Un job appartenant à quelqu'un d'autre renvoie `404`, pas `403` :
    confirmer son existence renseignerait déjà un tiers."""
    job = await session.get(GenerationJob, job_id)
    if job is None or job.external_user_id != principal.external_user_id:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message="Génération introuvable.",
            http_status=status.HTTP_404_NOT_FOUND,
        )

    plan = None
    if job.plan_id is not None:
        complet = await _plan_complet(session, job.plan_id)
        if complet:
            regles = await _regles(session)
            plan = _plan_out(complet, set(regles.meal_distribution))

    return JobOut(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_code=job.error_code,
        error_detail=job.error_detail,
        plan=plan,
    )


@router.get(
    "/meal-plans",
    response_model=PlanPage,
    summary="FN-025 — historique de vos programmes",
)
async def list_plans(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PlanPage:
    base = select(MealPlan).where(
        MealPlan.external_user_id == principal.external_user_id
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.scalars(
        base.order_by(MealPlan.created_at.desc()).limit(limit).offset(offset)
    )
    return PlanPage(
        total=total or 0,
        items=[
            PlanSummary(
                id=p.id,
                status=p.status,
                goal=p.goal,
                start_date=p.start_date,
                end_date=p.end_date,
                days_count=p.days_count,
                kcal_target=p.kcal_target,
                version=p.version,
                created_at=p.created_at,
            )
            for p in rows
        ],
    )
