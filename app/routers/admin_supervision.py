"""Supervision — API du tableau de bord (FN-035, plan §6).

Lecture seule. Ouverte aux administrateurs et aux nutritionnistes
(`require_catalog_editor`).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_catalog_editor
from app.core.database import get_session
from app.models.audit import AuditLog
from app.models.catalog import Dish
from app.models.planning import (
    GenerationJob,
    MealPlan,
    PlanValidation,
    RecommendationCandidate,
    RecommendationRun,
)
from app.routers.pilotage_commun import RoutePilotage, introuvable
from app.services import supervision

router = APIRouter(
    prefix="/api/v1/admin/supervision",
    tags=["pilotage — supervision"],
    dependencies=[Depends(require_catalog_editor)],
    route_class=RoutePilotage,
)


def _duree_ms(job: GenerationJob) -> int | None:
    if job.started_at and job.finished_at:
        return int((job.finished_at - job.started_at).total_seconds() * 1000)
    return None


@router.get("/indicateurs", summary="FN-035 — indicateurs mesurés")
async def indicateurs(
    periode: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await supervision.mesurer(session, periode_jours=periode)


@router.get("/generations", summary="Dernières générations")
async def generations(
    limite: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    jobs = await session.scalars(
        select(GenerationJob).order_by(GenerationJob.created_at.desc()).limit(limite)
    )
    return {
        "generations": [
            {
                "job_id": str(j.id),
                "status": j.status.value,
                "created_at": j.created_at.isoformat(),
                "duree_ms": _duree_ms(j),
                "error_code": j.error_code,
                "error_detail": j.error_detail,
                "goal": (j.request_params or {}).get("goal"),
                "days": (j.request_params or {}).get("days"),
                "plan_id": str(j.plan_id) if j.plan_id else None,
                "utilisateur": supervision.masquer_utilisateur(j.external_user_id),
            }
            for j in jobs
        ]
    }


@router.get("/generations/{job_id}", summary="Trace complète d'une génération")
async def generation_detail(
    job_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    job = await session.get(GenerationJob, job_id)
    if job is None:
        raise introuvable("Génération introuvable.")

    plan = await session.get(MealPlan, job.plan_id) if job.plan_id else None
    run = (
        await session.scalar(select(RecommendationRun).where(RecommendationRun.plan_id == plan.id))
        if plan
        else None
    )
    candidats = (
        (
            await session.execute(
                select(RecommendationCandidate, Dish.name)
                .outerjoin(Dish, Dish.id == RecommendationCandidate.dish_id)
                .where(RecommendationCandidate.run_id == run.id)
                .order_by(RecommendationCandidate.day_index, RecommendationCandidate.slot)
            )
        ).all()
        if run
        else []
    )
    validations = (
        list(
            await session.scalars(
                select(PlanValidation)
                .where(PlanValidation.plan_id == plan.id)
                .order_by(PlanValidation.created_at)
            )
        )
        if plan
        else []
    )
    ressources = [str(job.id)] + ([str(plan.id)] if plan else [])
    journal = list(
        await session.scalars(
            select(AuditLog)
            .where(or_(*(AuditLog.resource_id == r for r in ressources)))
            .order_by(AuditLog.occurred_at)
            .limit(50)
        )
    )

    return {
        "job": {
            "job_id": str(job.id),
            "status": job.status.value,
            "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "duree_ms": _duree_ms(job),
            "error_code": job.error_code,
            "error_detail": job.error_detail,
            "request_params": job.request_params or {},
            "utilisateur": supervision.masquer_utilisateur(job.external_user_id),
        },
        "plan": plan
        and {
            "id": str(plan.id),
            "status": plan.status.value,
            "goal": plan.goal.value,
            "days_count": plan.days_count,
            "kcal_target": float(plan.kcal_target),
            "version": plan.version,
        },
        "run": run
        and {
            "scoring_version": run.scoring_version,
            "random_seed": run.random_seed,
            "llm_call_count": run.llm_call_count,
            "used_fallback": run.used_fallback,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "cost": float(run.cost),
            "latency_ms": run.latency_ms,
        },
        "candidats": [
            {
                "jour": c.day_index,
                "slot": c.slot.value,
                "plat": nom,
                "score": float(c.score),
                "detail": c.score_breakdown or {},
                "rang": c.rank,
                "retenu": c.selected,
            }
            for c, nom in candidats
        ],
        "validations": [
            {
                "phase": v.phase,
                "outcome": v.outcome.value,
                "failed_checks": list(v.failed_checks or []),
                "incident": v.is_food_safety_incident,
                "at": v.created_at.isoformat(),
            }
            for v in validations
        ],
        "journal": [
            {
                "at": a.occurred_at.isoformat(),
                "action": a.action,
                "resultat": a.result.value,
                "details": a.details or {},
            }
            for a in journal
        ],
    }
