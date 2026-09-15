"""Banc d'essai HTML interne (FN-034 — amorce du lot 1).

Elle couvre les pages du lot 1 attendues par FN-034 — catalogue d'ingrédients,
saisie et validation d'ingrédient, catalogue de plats, saisie et validation de
plat, file de validation — et sert aussi de banc d'essai des règles pures :

* sondes `/health` et `/ready`, propagation de `X-Request-ID` (FN-036, FN-037) ;
* conversions d'unités, y compris locales (FN-012) ;
* dérivations d'un plat — valeurs nutritionnelles, allergènes, tags,
  restrictions compatibles, verdict de publication (D-11, FN-003, FN-008,
  FN-009) ;
* validation et application du catalogue versionné (FN-040) ;
* lecture du catalogue en base et **comparaison au recalcul** (D-11) ;
* enveloppe d'erreur et codes stables (FN-037) ;
* joignabilité du fournisseur IA ;
* émission d'un jeton de développement, et **refus** attendus (HS256,
  audience incorrecte, expiration) — D-03 ;
* profil, préférences, allergies et restrictions (FN-001 → FN-004) ;
* saisie du catalogue, signature des allergènes et file de validation
  (FN-007 → FN-009) ;
* **avancement mesuré du lot 1** (§15), recalculé à chaque affichage.

Elle est **refusée en production** : outil de développement non authentifié,
qui expose l'état interne du service.
"""

import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Principal, current_principal
from app.core.config import settings
from app.core.database import create_sync_session_factory, engine, get_session
from app.core.errors import DEFAULT_MESSAGES, ErrorCode, NutritionError
from app.models.catalog import Dish, DishIngredient, Ingredient
from app.models.enums import (
    Allergen,
    DishStatus,
    IngredientCategory,
    ReferenceUnit,
    RestrictionFlag,
    RestrictionType,
)
from app.schemas.console import DeriveIn, DevTokenIn, UnitConvertIn
from app.seed.loader import build_facts, parse_dishes, parse_ingredients, run_seed
from app.services.nutrition import DishComponent, IngredientFacts, derive_dish_data
from app.services.progress import lot1_report
from app.services.units import (
    IngredientConversion,
    UnitConversionError,
    to_reference_quantity,
)

SEEDS_DIR = Path(__file__).resolve().parents[2] / "seeds"
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: Statut HTTP associé à chaque code, pour le déclencheur d'erreurs. Les codes
#: fonctionnels de FN-037 restent des 4xx métier : ils décrivent une situation
#: que l'utilisateur peut corriger, pas une panne du service.
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHENTICATED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    ErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
    ErrorCode.QUOTA_EXCEEDED: status.HTTP_429_TOO_MANY_REQUESTS,
    # `HTTP_422_UNPROCESSABLE_ENTITY` est déprécié dans Starlette et émet un
    # avertissement dès l'import ; `..._CONTENT` porte la même valeur (422).
    ErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.SERVICE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def dev_only() -> None:
    """La console n'existe pas en production — 404 et non 403 : rien ne doit
    laisser deviner qu'un outil interne est déployé à cette adresse."""
    if settings.is_production:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(prefix="/console", tags=["console"], dependencies=[Depends(dev_only)])


# --------------------------------------------------------------------------
# Passerelles vers les services purs
# --------------------------------------------------------------------------


def _as_validation_error(message: str) -> NutritionError:
    """Une erreur de saisie du banc d'essai reste une erreur d'API normale :
    même enveloppe, même code stable (FN-037)."""
    return NutritionError(
        ErrorCode.VALIDATION_ERROR,
        message=message,
        http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


def _unavailable(exc: Exception) -> NutritionError:
    return NutritionError(
        ErrorCode.SERVICE_UNAVAILABLE,
        message=f"base indisponible ({type(exc).__name__})",
        http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        details={"exception": repr(exc)},
    )


def _facts_from_row(row: Ingredient) -> IngredientFacts:
    """Ingrédient en base vers vue de calcul. Les colonnes tableau sont stockées
    en `text[]` borné par contrainte ; elles sont retypées ici."""
    return IngredientFacts(
        slug=row.slug,
        reference_unit=ReferenceUnit(row.reference_unit),
        kcal_100=Decimal(row.kcal_100),
        protein_100=Decimal(row.protein_100),
        carbs_100=Decimal(row.carbs_100),
        fat_100=Decimal(row.fat_100),
        fiber_100=Decimal(row.fiber_100),
        allergens=frozenset(Allergen(a) for a in row.allergens),
        restriction_flags=frozenset(RestrictionFlag(f) for f in row.restriction_flags),
        density_g_per_ml=(
            Decimal(row.density_g_per_ml) if row.density_g_per_ml is not None else None
        ),
        conversions=tuple(
            IngredientConversion(c.from_unit, c.to_unit, Decimal(c.factor))
            for c in row.unit_conversions
        ),
        is_allergen_verified=row.is_allergen_verified,
    )


def _derived_payload(derived: Any) -> dict[str, Any]:
    return {
        "nutrition": {
            "kcal": derived.nutrition.kcal,
            "protein_g": derived.nutrition.protein_g,
            "carbs_g": derived.nutrition.carbs_g,
            "fat_g": derived.nutrition.fat_g,
            "fiber_g": derived.nutrition.fiber_g,
        },
        "allergens": sorted(a.value for a in derived.allergens),
        "derived_tags": sorted(t.value for t in derived.derived_tags),
        "compatible_restrictions": sorted(
            r.value for r in derived.compatible_restrictions
        ),
        "unverified_ingredients": list(derived.unverified_ingredients),
        # FN-003 : c'est cette ligne, et elle seule, qui autorise la publication.
        "publishable": not derived.unverified_ingredients,
    }


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse, summary="Banc d'essai (page)")
async def console_page(request: Request) -> HTMLResponse:
    """Les listes déroulantes sont peuplées depuis les énumérations Python : la
    page ne peut pas proposer une valeur que le domaine ne connaît pas."""
    return templates.TemplateResponse(
        request,
        "console.html",
        {
            "environment": settings.environment,
            "llm_model": settings.llm_model,
            "allergens": [a.value for a in Allergen],
            "restriction_flags": [f.value for f in RestrictionFlag],
            "restriction_types": [r.value for r in RestrictionType],
            "reference_units": [u.value for u in ReferenceUnit],
            "ingredient_categories": [c.value for c in IngredientCategory],
            "dish_statuses": [s.value for s in DishStatus],
            "error_codes": [
                {
                    "code": code.value,
                    "message": DEFAULT_MESSAGES[code],
                    "http_status": ERROR_HTTP_STATUS.get(
                        code, status.HTTP_400_BAD_REQUEST
                    ),
                }
                for code in ErrorCode
            ],
        },
    )


# --------------------------------------------------------------------------
# Vue d'ensemble
# --------------------------------------------------------------------------


@router.get("/api/overview", summary="État consolidé du service")
async def overview() -> dict[str, Any]:
    """Ne remonte jamais d'erreur : c'est l'en-tête d'état de la page, il doit
    s'afficher même base coupée."""
    database: dict[str, Any]
    catalog: dict[str, Any] = {}

    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            ).scalar_one_or_none()
            ingredients_total = (
                await connection.execute(text("SELECT count(*) FROM ingredients"))
            ).scalar_one()
            ingredients_verified = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM ingredients"
                        " WHERE allergen_source IS NOT NULL"
                        " AND allergen_verified_by IS NOT NULL"
                        " AND allergen_verified_at IS NOT NULL"
                    )
                )
            ).scalar_one()
            by_status = (
                await connection.execute(
                    text("SELECT status, count(*) FROM dishes GROUP BY status")
                )
            ).all()

        database = {
            "status": "up",
            "migration": revision,
            "name": settings.database_name,
        }
        catalog = {
            "ingredients_total": ingredients_total,
            "ingredients_verified": ingredients_verified,
            # FN-035 : cet indicateur doit finir à zéro. Il ne l'est pas encore.
            "ingredients_unverified": ingredients_total - ingredients_verified,
            "dishes_by_status": {row[0]: row[1] for row in by_status},
        }
    except Exception as exc:  # noqa: BLE001 — la console affiche la cause
        database = {"status": "down", "reason": type(exc).__name__}

    return {
        "service": {
            "environment": settings.environment,
            "log_level": settings.log_level,
            "seeds_dir": str(SEEDS_DIR),
        },
        "database": database,
        "catalog": catalog,
        "llm": {
            "model": settings.llm_model,
            "key_configured": bool(settings.open_router_key.get_secret_value()),
            "timeout_seconds": settings.llm_timeout_seconds,
        },
        "auth": {
            "jwks_url": settings.jwt_jwks_url,
            "issuer": settings.jwt_issuer,
            "audience": settings.jwt_audience,
            # §4.3 : la vérification n'est pas écrite et aucun point d'entrée
            # n'est protégé. La console ne prétend pas le contraire.
            "verification_implemented": False,
        },
    }


# --------------------------------------------------------------------------
# FN-012 — conversion d'unités
# --------------------------------------------------------------------------


def _conversion_rule(
    payload: UnitConvertIn, conversions: list[IngredientConversion]
) -> str:
    """Rend explicite *laquelle* des trois règles de FN-012 a été appliquée."""
    unit = payload.unit.strip().lower()
    target = payload.reference_unit.value
    if unit == target:
        return "identité — aucune conversion"
    for conversion in conversions:
        if (
            conversion.from_unit.strip().lower() == unit
            and conversion.to_unit.strip().lower() == target
        ):
            return (
                "conversion mesurée propre à l'ingrédient "
                f"(facteur {conversion.factor})"
            )
    if payload.density_g_per_ml is not None:
        return "conversion dimensionnelle, ou masse vers volume par la densité"
    return "conversion dimensionnelle universelle"


@router.post("/api/units/convert", summary="FN-012 — convertir une quantité")
async def convert_unit(payload: UnitConvertIn) -> dict[str, Any]:
    conversions = [
        IngredientConversion(c.from_unit, c.to_unit, c.factor)
        for c in payload.conversions
    ]
    try:
        quantity = to_reference_quantity(
            payload.quantity,
            payload.unit,
            payload.reference_unit,
            density_g_per_ml=payload.density_g_per_ml,
            conversions=conversions,
        )
    except UnitConversionError as exc:
        # FN-012 : une conversion absente bloque et le signale. La console doit
        # montrer ce refus, pas le contourner.
        raise _as_validation_error(str(exc)) from exc

    return {
        "input": {
            "quantity": payload.quantity,
            "unit": payload.unit,
            "reference_unit": payload.reference_unit.value,
        },
        "reference_quantity": quantity,
        "reference_unit": payload.reference_unit.value,
        "rule": _conversion_rule(payload, conversions),
    }


# --------------------------------------------------------------------------
# D-11 / FN-003 / FN-008 / FN-009 — dérivations d'un plat
# --------------------------------------------------------------------------


@router.post("/api/dishes/derive", summary="D-11 — dériver un plat de sa composition")
def derive(payload: DeriveIn) -> dict[str, Any]:
    """Point d'entrée synchrone : le calcul est pur et la lecture des YAML est du
    disque local. FastAPI l'exécute dans un fil dédié, la boucle reste libre."""
    if not payload.components:
        raise _as_validation_error("aucun ingrédient : un plat vide ne dérive rien")

    seed_ingredients, errors = parse_ingredients(SEEDS_DIR / "ingredients")
    if errors:
        raise _as_validation_error("catalogue de seed invalide : " + " | ".join(errors))

    components: list[DishComponent] = []
    for entry in payload.components:
        if entry.custom is not None:
            facts = IngredientFacts(
                slug=entry.custom.slug,
                reference_unit=entry.custom.reference_unit,
                kcal_100=entry.custom.kcal_100,
                protein_100=entry.custom.protein_100,
                carbs_100=entry.custom.carbs_100,
                fat_100=entry.custom.fat_100,
                fiber_100=entry.custom.fiber_100,
                allergens=frozenset(entry.custom.allergens),
                restriction_flags=frozenset(entry.custom.restriction_flags),
                density_g_per_ml=entry.custom.density_g_per_ml,
                conversions=tuple(
                    IngredientConversion(c.from_unit, c.to_unit, c.factor)
                    for c in entry.custom.conversions
                ),
                is_allergen_verified=entry.custom.is_allergen_verified,
            )
        else:
            seed = seed_ingredients.get(entry.ingredient or "")
            if seed is None:
                raise _as_validation_error(
                    f"ingrédient inconnu du catalogue : {entry.ingredient}"
                )
            facts = build_facts(seed)

        components.append(
            DishComponent(ingredient=facts, quantity=entry.quantity, unit=entry.unit)
        )

    try:
        derived = derive_dish_data(components, payload.servings)
    except UnitConversionError as exc:
        raise _as_validation_error(str(exc)) from exc
    except ValueError as exc:  # servings <= 0
        raise _as_validation_error(str(exc)) from exc

    return {
        "servings": payload.servings,
        # Le détail par ingrédient rend le total vérifiable à la main : sans lui,
        # une valeur fausse ne se distingue pas d'une valeur juste.
        "components": [
            {
                "ingredient": component.ingredient.slug,
                "quantity": component.quantity,
                "unit": component.unit,
                "reference_quantity": to_reference_quantity(
                    component.quantity,
                    component.unit,
                    component.ingredient.reference_unit,
                    density_g_per_ml=component.ingredient.density_g_per_ml,
                    conversions=list(component.ingredient.conversions),
                ),
                "reference_unit": component.ingredient.reference_unit.value,
                "allergens": sorted(a.value for a in component.ingredient.allergens),
                "allergen_verified": component.ingredient.is_allergen_verified,
            }
            for component in components
        ],
        "derived": _derived_payload(derived),
    }


# --------------------------------------------------------------------------
# FN-040 — catalogue versionné
# --------------------------------------------------------------------------


@router.get("/api/seed/catalogue", summary="FN-040 — contenu des fichiers de seed")
def seed_catalogue() -> dict[str, Any]:
    """Lit les YAML sans base : c'est ce qui alimente les sélecteurs de la page,
    et cela reste disponible quand PostgreSQL ne répond pas."""
    ingredients, ingredient_errors = parse_ingredients(SEEDS_DIR / "ingredients")
    dishes, dish_errors = parse_dishes(SEEDS_DIR / "dishes")

    return {
        "errors": ingredient_errors + dish_errors,
        "ingredients": [
            {
                "slug": seed.slug,
                "name": seed.name,
                "category": seed.category.value,
                "reference_unit": seed.reference_unit.value,
                "kcal_100": seed.nutrition.kcal_100,
                "protein_100": seed.nutrition.protein_100,
                "carbs_100": seed.nutrition.carbs_100,
                "fat_100": seed.nutrition.fat_100,
                "fiber_100": seed.nutrition.fiber_100,
                "allergens": [a.value for a in seed.allergens.values],
                "allergen_verified": seed.allergens.is_verified,
                "restriction_flags": [f.value for f in seed.restriction_flags],
                "density_g_per_ml": seed.density_g_per_ml,
                # Unités que cet ingrédient sait effectivement recevoir.
                "units": sorted(
                    {seed.reference_unit.value}
                    | {c.from_unit for c in seed.unit_conversions}
                ),
                "conversions": [
                    {
                        "from_unit": c.from_unit,
                        "to_unit": c.to_unit,
                        "factor": c.factor,
                        "source": c.source,
                    }
                    for c in seed.unit_conversions
                ],
            }
            for seed in ingredients.values()
        ],
        "dishes": [
            {
                "slug": seed.slug,
                "name": seed.name,
                "servings": seed.servings,
                "meal_types": [m.value for m in seed.meal_types],
                "publish_requested": seed.validation.publish,
                "ingredients": [
                    {
                        "ingredient": entry.ingredient,
                        "quantity": entry.quantity,
                        "unit": entry.unit,
                    }
                    for entry in seed.ingredients
                ],
            }
            for seed in dishes.values()
        ],
    }


def _seed_report_payload(report: Any, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "ok": report.ok,
        "ingredients_created": report.ingredients_created,
        "ingredients_updated": report.ingredients_updated,
        "dishes_created": report.dishes_created,
        "dishes_updated": report.dishes_updated,
        "dishes_published": report.dishes_published,
        "publication_blocked": [
            {"slug": slug, "reason": reason}
            for slug, reason in report.publication_blocked
        ],
        "errors": report.errors,
        "render": report.render(),
    }


@router.post("/api/seed/dry-run", summary="FN-040 — valider sans rien écrire")
def seed_dry_run() -> dict[str, Any]:
    report = run_seed(session=None, seeds_dir=SEEDS_DIR, dry_run=True)
    return _seed_report_payload(report, "dry-run")


@router.post("/api/seed/apply", summary="FN-040 — appliquer le seed (idempotent)")
def seed_apply() -> dict[str, Any]:
    """Équivalent de `python -m app.seed`. Rejouable à l'identique : la clé
    fonctionnelle est le `slug`, jamais un identifiant technique."""
    session_factory = create_sync_session_factory()
    with session_factory() as session:
        # Le moteur synchrone est créé à la demande ; il faut le libérer, sinon
        # chaque exécution laisserait un pool de connexions ouvert.
        sync_engine = session.get_bind()
        try:
            report = run_seed(session, SEEDS_DIR, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            sync_engine.dispose()
            raise _unavailable(exc) from exc
    sync_engine.dispose()
    return _seed_report_payload(report, "apply")


# --------------------------------------------------------------------------
# Catalogue en base
# --------------------------------------------------------------------------


@router.get("/api/catalog/ingredients", summary="FN-007 — ingrédients en base")
async def catalog_ingredients(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        rows = (
            await session.scalars(
                select(Ingredient)
                .options(
                    selectinload(Ingredient.unit_conversions),
                    selectinload(Ingredient.aliases),
                )
                .order_by(Ingredient.category, Ingredient.name)
            )
        ).all()
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc

    return {
        "total": len(rows),
        "items": [
            {
                "slug": row.slug,
                "name": row.name,
                "category": row.category.value,
                "status": row.status.value,
                "reference_unit": row.reference_unit.value,
                "kcal_100": row.kcal_100,
                "protein_100": row.protein_100,
                "carbs_100": row.carbs_100,
                "fat_100": row.fat_100,
                "fiber_100": row.fiber_100,
                "nutrition_source": row.nutrition_source,
                "allergens": row.allergens,
                # FN-003 : « vérifié » signifie signé, pas « sans allergène ».
                "allergen_verified": row.is_allergen_verified,
                "allergen_source": row.allergen_source,
                "restriction_flags": row.restriction_flags,
                "locally_available": row.locally_available,
                "density_g_per_ml": row.density_g_per_ml,
                "aliases": [
                    {"alias": a.alias, "language": a.language} for a in row.aliases
                ],
                "conversions": [
                    {
                        "from_unit": c.from_unit,
                        "to_unit": c.to_unit,
                        "factor": c.factor,
                        "source": c.source,
                    }
                    for c in row.unit_conversions
                ],
            }
            for row in rows
        ],
    }


def _dish_query():
    return select(Dish).options(
        selectinload(Dish.ingredients)
        .selectinload(DishIngredient.ingredient)
        .selectinload(Ingredient.unit_conversions),
        selectinload(Dish.steps),
        selectinload(Dish.tags),
        selectinload(Dish.allergens),
    )


def _dish_summary(dish: Dish) -> dict[str, Any]:
    return {
        "slug": dish.slug,
        "name": dish.name,
        "status": dish.status.value,
        "meal_types": dish.meal_types,
        "servings": dish.servings,
        "origin": dish.origin,
        "difficulty": dish.difficulty.value,
        "prep_time_min": dish.prep_time_min,
        "cook_time_min": dish.cook_time_min,
        "estimated_cost": dish.estimated_cost,
        "cost_class": dish.cost_class.value,
        "currency": dish.currency,
        "nutrition": {
            "kcal": dish.kcal_portion,
            "protein_g": dish.protein_portion,
            "carbs_g": dish.carbs_portion,
            "fat_g": dish.fat_portion,
            "fiber_g": dish.fiber_portion,
        },
        "nutrition_computed_at": dish.nutrition_computed_at,
        "allergens": sorted(a.allergen.value for a in dish.allergens),
        "compatible_restrictions": dish.compatible_restrictions,
        "compatible_goals": dish.compatible_goals,
        "tags_derived": sorted(t.tag.value for t in dish.tags if t.is_derived),
        "tags_manual": sorted(t.tag.value for t in dish.tags if not t.is_derived),
        "author_id": dish.author_id,
        "validated_by": dish.validated_by,
        "published_at": dish.published_at,
        "steps": [step.instruction for step in dish.steps],
        "ingredients": [
            {
                "ingredient": link.ingredient.slug,
                "name": link.ingredient.name,
                "quantity": link.quantity,
                "unit": link.unit,
                "allergen_verified": link.ingredient.is_allergen_verified,
            }
            for link in sorted(dish.ingredients, key=lambda i: i.display_order)
        ],
    }


@router.get("/api/catalog/dishes", summary="FN-008 — plats en base")
async def catalog_dishes(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        rows = (await session.scalars(_dish_query().order_by(Dish.name))).all()
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc

    return {"total": len(rows), "items": [_dish_summary(dish) for dish in rows]}


@router.get(
    "/api/catalog/dishes/{slug}",
    summary="D-11 — plat en base, confronté à son recalcul",
)
async def catalog_dish_detail(
    slug: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Le contrôle qui compte : les valeurs stockées doivent être exactement
    celles que `derive_dish_data` produit depuis les ingrédients. Tout écart
    signale une valeur saisie à la main, ou un recalcul oublié."""
    try:
        dish = await session.scalar(_dish_query().where(Dish.slug == slug))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc

    if dish is None:
        raise NutritionError(
            ErrorCode.NOT_FOUND,
            message=f"aucun plat de slug « {slug} »",
            http_status=status.HTTP_404_NOT_FOUND,
        )

    stored = _dish_summary(dish)
    components = [
        DishComponent(
            ingredient=_facts_from_row(link.ingredient),
            quantity=Decimal(link.quantity),
            unit=link.unit,
        )
        for link in sorted(dish.ingredients, key=lambda i: i.display_order)
    ]

    try:
        derived = derive_dish_data(components, dish.servings)
    except UnitConversionError as exc:
        return {
            "dish": stored,
            "recomputed": None,
            "differences": [f"recalcul impossible — {exc}"],
        }

    recomputed = _derived_payload(derived)
    differences: list[str] = []

    for key, value in recomputed["nutrition"].items():
        reference = stored["nutrition"][key]
        if reference is None or Decimal(reference) != value:
            differences.append(f"{key} — base : {reference} · recalcul : {value}")

    if stored["allergens"] != recomputed["allergens"]:
        differences.append(
            f"allergènes — base : {stored['allergens']} · "
            f"recalcul : {recomputed['allergens']}"
        )
    if sorted(stored["compatible_restrictions"]) != recomputed["compatible_restrictions"]:
        differences.append(
            "restrictions compatibles — base : "
            f"{sorted(stored['compatible_restrictions'])} · "
            f"recalcul : {recomputed['compatible_restrictions']}"
        )
    if stored["tags_derived"] != recomputed["derived_tags"]:
        differences.append(
            f"tags dérivés — base : {stored['tags_derived']} · "
            f"recalcul : {recomputed['derived_tags']}"
        )

    return {"dish": stored, "recomputed": recomputed, "differences": differences}


# --------------------------------------------------------------------------
# FN-037 — enveloppe d'erreur
# --------------------------------------------------------------------------


@router.get(
    "/api/errors/unhandled",
    summary="FN-037 — exception non prévue (doit devenir un 500 générique)",
)
async def trigger_unhandled() -> None:
    """Vérifie qu'aucun détail technique ne fuit : le message ci-dessous est
    journalisé, le client ne reçoit que l'enveloppe générique.

    Déclarée avant `/{code}` : FastAPI résout les routes dans l'ordre, et
    `unhandled` n'est pas un `ErrorCode` valide.
    """
    raise RuntimeError("défaut simulé — ce texte ne doit pas atteindre le client")


@router.get("/api/errors/{code}", summary="FN-037 — déclencher un code d'erreur")
async def trigger_error(code: ErrorCode) -> None:
    raise NutritionError(
        code,
        http_status=ERROR_HTTP_STATUS.get(code, status.HTTP_400_BAD_REQUEST),
        details={"origin": "banc d'essai"},
    )


# --------------------------------------------------------------------------
# D-03 — jeton de développement
# --------------------------------------------------------------------------


@router.post("/api/auth/dev-token", summary="D-03 — émettre un jeton de test")
def dev_token(payload: DevTokenIn) -> dict[str, Any]:
    """Signe un jeton avec la clé privée **de développement**.

    Ce point d'entrée n'existe que hors production, et `Settings` refuse déjà
    de démarrer si une clé de signature est configurée en production : le
    service ARISE ne doit jamais pouvoir forger un jeton (piège n° 4).

    Les options `force_hs256`, `wrong_audience`, `wrong_issuer` et `expired`
    produisent des jetons que la vérification doit rejeter. Elles sont là pour
    qu'on puisse *voir* le refus, pas seulement le supposer.
    """
    import jwt as pyjwt

    if not settings.jwt_dev_private_key_path:
        raise NutritionError(
            ErrorCode.SERVICE_UNAVAILABLE,
            message=(
                "Aucune clé de développement configurée. Exécuter "
                "`python scripts/generate_dev_keys.py`, puis renseigner "
                "JWT_DEV_PRIVATE_KEY_PATH dans .env."
            ),
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    key_path = Path(settings.jwt_dev_private_key_path)
    if not key_path.is_absolute():
        key_path = Path(__file__).resolve().parents[2] / key_path
    if not key_path.exists():
        raise NutritionError(
            ErrorCode.SERVICE_UNAVAILABLE,
            message=f"Clé de développement introuvable : {key_path}",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    now = int(time.time())
    ttl = settings.jwt_dev_token_ttl_seconds
    claims = {
        "sub": payload.sub,
        "role": payload.role,
        "entitlements": payload.entitlements,
        "iss": "un-autre-emetteur" if payload.wrong_issuer else settings.jwt_issuer,
        "aud": ["une-autre-audience"] if payload.wrong_audience else [settings.jwt_audience],
        "iat": now - (ttl + 60) if payload.expired else now,
        "exp": now - 60 if payload.expired else now + ttl,
    }

    if payload.force_hs256:
        # Signature symétrique : la vérification doit la refuser sur
        # l'algorithme, avant même de regarder la signature.
        token = pyjwt.encode(claims, "secret-partage-de-test", algorithm="HS256")
        expectation = "doit être refusé — algorithme HS256"
    else:
        token = pyjwt.encode(
            claims, key_path.read_text(encoding="utf-8"), algorithm="RS256"
        )
        expectation = None
        if payload.wrong_audience:
            expectation = "doit être refusé — audience incorrecte"
        elif payload.wrong_issuer:
            expectation = "doit être refusé — émetteur incorrect"
        elif payload.expired:
            expectation = "doit être refusé — jeton expiré"

    return {
        "token": token,
        "claims": claims,
        "algorithm": "HS256" if payload.force_hs256 else "RS256",
        "expires_in": 0 if payload.expired else ttl,
        "expectation": expectation or "doit être accepté",
    }


@router.get("/api/auth/whoami", summary="Vérifier un jeton et lire ses claims")
async def whoami(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Passe par exactement la même dépendance que les routes métier : ce qui
    est accepté ici est accepté partout, et réciproquement."""
    return {
        "external_user_id": principal.external_user_id,
        "role": principal.role,
        "entitlements": list(principal.entitlements),
        "issued_at": principal.issued_at,
        "expires_at": principal.expires_at,
        "verified_with": "clé de développement locale" if principal.dev_key else "JWKS",
    }


# --------------------------------------------------------------------------
# §15 — avancement mesuré du lot 1
# --------------------------------------------------------------------------


@router.get("/api/progress", summary="§15 — avancement du lot 1, mesuré")
async def progress(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Chaque critère est vérifié à l'exécution : base interrogée, routes
    introspectées, JWKS sondé. Rien n'est déclaré."""
    try:
        report = await lot1_report(session, request.app)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    return report.as_dict()
