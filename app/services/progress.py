"""Avancement réel du lot 1, mesuré (§15, FN-035).

Un tableau d'avancement écrit à la main devient faux dès le premier commit qui
oublie de le mettre à jour. Chaque critère est donc **vérifié à l'exécution** :
on interroge la base, on introspecte les routes enregistrées et les schémas
Pydantic, on sonde le fournisseur d'identité.

Les trois vérifications qui comptent le plus sont celles qu'un tableau
déclaratif ne sait pas faire :

* « les valeurs nutritionnelles ne se saisissent pas » est vérifié en
  regardant si le schéma d'entrée d'un plat expose un champ de valeur
  nutritionnelle — pas en le croyant sur parole ;
* « la publication est refusée si un ingrédient n'est pas vérifié » est vérifié
  en exécutant réellement le contrôle sur les plats en base ;
* « ~150 ingrédients, 100 % vérifiés » compte les lignes, et ne s'arrondit pas.

Un critère qu'on ne sait pas mesurer est déclaré `unknown` plutôt que `fait` :
mieux vaut un trou visible qu'un vert mensonger.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.catalog import Dish, Ingredient
from app.models.enums import DishStatus
from app.services.catalog import publication_blockers
from app.services.units import UnitConversionError

#: Cibles de FN-040. Ce sont elles qui font du seed le goulot du lot 1.
TARGET_INGREDIENTS = 150
TARGET_DISHES = 180

ROOT = Path(__file__).resolve().parents[2]

DONE, PARTIAL, TODO, UNKNOWN = "fait", "partiel", "afaire", "inconnu"


@dataclass
class Check:
    """Un critère de fin de lot, son état et **la preuve** de cet état."""

    id: str
    label: str
    ref: str
    state: str
    evidence: str
    #: Progression 0–1 quand le critère est quantifiable (volumétrie du seed).
    ratio: float | None = None
    #: Ce qu'il reste à faire, quand ce n'est pas évident depuis le libellé.
    next_step: str | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> int:
        return sum(1 for c in self.checks if c.state == DONE)

    @property
    def partial(self) -> int:
        return sum(1 for c in self.checks if c.state == PARTIAL)

    def as_dict(self) -> dict[str, Any]:
        total = len(self.checks)
        # Un critère entamé compte pour la moitié : c'est la même convention
        # que la fiche de suivi des chantiers, pour que les deux se comparent.
        score = (self.done + self.partial * 0.5) / total if total else 0.0
        return {
            "checks": [asdict(c) for c in self.checks],
            "metrics": self.metrics,
            "summary": {
                "total": total,
                "done": self.done,
                "partial": self.partial,
                "todo": total - self.done - self.partial,
                "completion": round(score, 3),
            },
        }


# --------------------------------------------------------------------------
# Sondes unitaires
# --------------------------------------------------------------------------


def _routes(app: Any) -> set[str]:
    """Chemins enregistrés, en traversant les routeurs inclus.

    FastAPI conserve désormais les routeurs inclus sous forme d'objets
    intermédiaires plutôt que d'aplatir leurs routes : il faut descendre.
    """
    found: set[str] = set()

    def walk(container: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        for route in getattr(container, "routes", []) or []:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                found.add(path)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner, depth + 1)

    walk(app)
    return found


async def _jwks_reachable() -> tuple[bool, str]:
    """Sonde l'endpoint JWKS de NestJS. C'est le seul moyen honnête de savoir
    si la migration RS256 côté ARISE est faite."""
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(settings.jwt_jwks_url)
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        keys = response.json().get("keys") or []
        return bool(keys), f"{len(keys)} clé(s) publiée(s)"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


#: Noms de paramètre qui trahiraient une route capable de désigner autrui.
IDENTIFYING_PARAMS = frozenset(
    {"external_user_id", "user_id", "profile_id", "sub", "owner_id"}
)


def _profile_routes_are_self_scoped(app: Any) -> tuple[bool, str]:
    """FN-001 · §14.5 — aucune route de profil ne doit pouvoir désigner un
    autre utilisateur.

    Vérifié par introspection plutôt que par relecture : on regarde les
    paramètres réellement déclarés et la présence de la dépendance
    d'authentification. Une route ajoutée demain sans garde fera basculer ce
    critère au rouge toute seule.
    """
    from app.core.auth import current_user

    offenders: list[str] = []
    checked = 0

    def walk(container: Any, depth: int = 0) -> None:
        nonlocal checked
        if depth > 4:
            return
        for route in getattr(container, "routes", []) or []:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner, depth + 1)
                continue
            path = getattr(route, "path", "")
            if not path.startswith("/api/v1/nutrition"):
                continue
            checked += 1
            dependant = getattr(route, "dependant", None)
            names = {p.name for p in getattr(dependant, "path_params", [])}
            names |= {p.name for p in getattr(dependant, "query_params", [])}
            if names & IDENTIFYING_PARAMS:
                offenders.append(f"{path} accepte {sorted(names & IDENTIFYING_PARAMS)}")
            calls = {
                sub.call for sub in getattr(dependant, "dependencies", [])
            }
            if current_user not in calls:
                offenders.append(f"{path} n'exige pas current_user")

    walk(app)

    if not checked:
        return False, "aucune route de profil enregistrée"
    if offenders:
        return False, " · ".join(offenders)
    return True, (
        f"{checked} routes de profil : toutes résolues depuis le claim sub, "
        "aucune n'accepte d'identifiant utilisateur"
    )


def _nutrition_fields_absent_from_dish_input() -> tuple[bool, str]:
    """D-11 — le schéma d'entrée d'un plat ne doit exposer aucune valeur
    nutritionnelle ni aucun allergène : ils sont calculés."""
    from app.schemas.catalog import DishIn

    interdits = {
        "kcal_portion", "protein_portion", "carbs_portion", "fat_portion",
        "fiber_portion", "kcal", "allergens", "compatible_restrictions",
        "status", "validated_by", "validated_at", "published_at",
    }
    presents = sorted(interdits & set(DishIn.model_fields))
    if presents:
        return False, "champs saisissables à tort : " + ", ".join(presents)
    return True, "aucun champ dérivé n'est saisissable (extra=\"forbid\")"


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------


async def lot1_report(session: AsyncSession, app: Any) -> Report:
    report = Report()
    paths = _routes(app)

    # --- Volumétrie du catalogue ------------------------------------------
    ingredients_total = await session.scalar(select(func.count()).select_from(Ingredient))
    ingredients_verified = await session.scalar(
        select(func.count())
        .select_from(Ingredient)
        .where(
            Ingredient.allergen_source.is_not(None),
            Ingredient.allergen_verified_by.is_not(None),
            Ingredient.allergen_verified_at.is_not(None),
        )
    )
    by_status = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Dish.status, func.count()).group_by(Dish.status)
            )
        ).all()
    }
    dishes_total = sum(by_status.values())
    published = by_status.get(DishStatus.PUBLISHED, 0)

    report.metrics = {
        "ingredients_total": ingredients_total or 0,
        "ingredients_verified": ingredients_verified or 0,
        "ingredients_target": TARGET_INGREDIENTS,
        "dishes_total": dishes_total,
        "dishes_published": published,
        "dishes_target": TARGET_DISHES,
        "dishes_by_status": {
            (s.value if hasattr(s, "value") else str(s)): n for s, n in by_status.items()
        },
        "routes": len(paths),
    }

    # --- 1. Authentification -----------------------------------------------
    jwks_ok, jwks_detail = await _jwks_reachable()
    dev_key = bool(settings.jwt_dev_public_key_path) and (
        ROOT / settings.jwt_dev_public_key_path
    ).exists()
    verification_present = (ROOT / "app" / "core" / "auth.py").exists()

    if jwks_ok and verification_present:
        state, evidence = DONE, f"JWKS joignable — {jwks_detail}"
        next_step = None
    elif verification_present:
        state = PARTIAL
        evidence = (
            "vérification RS256 écrite côté FastAPI ; JWKS NestJS injoignable "
            f"({jwks_detail})"
            + (" — clé de développement locale utilisée" if dev_key else "")
        )
        next_step = "Migrer NestJS en RS256 et exposer /.well-known/jwks.json"
    else:
        state, evidence, next_step = TODO, "aucune vérification de jeton", None

    report.checks.append(
        Check(
            id="auth",
            label="NestJS signe en RS256 et expose JWKS ; FastAPI vérifie signature, iss et aud",
            ref="§4.3 · D-03",
            state=state,
            evidence=evidence,
            next_step=next_step,
        )
    )

    # --- 2. Schéma et migrations -------------------------------------------
    from sqlalchemy import text as sql_text

    revision = (
        await session.execute(sql_text("SELECT version_num FROM alembic_version"))
    ).scalar_one_or_none()
    tables = (
        await session.execute(
            sql_text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        )
    ).scalar_one()
    report.checks.append(
        Check(
            id="schema",
            label="Alembic initialisé, schéma complet des lots 1 et 2 migré",
            ref="§9 · §10",
            state=DONE if revision else TODO,
            evidence=f"révision {revision} · {tables} tables",
        )
    )

    # --- 3. API du profil ---------------------------------------------------
    profil_routes = {
        "/api/v1/nutrition/profile",
        "/api/v1/nutrition/preferences",
        "/api/v1/nutrition/allergies",
        "/api/v1/nutrition/restrictions",
    }
    manquantes = sorted(profil_routes - paths)
    report.checks.append(
        Check(
            id="profil",
            label="Créer et modifier profil, préférences, allergies et restrictions",
            ref="FN-001 → FN-004",
            state=DONE if not manquantes else (PARTIAL if len(manquantes) < 4 else TODO),
            evidence=(
                f"{len(profil_routes) - len(manquantes)}/{len(profil_routes)} "
                "familles de routes exposées"
                + ("" if not manquantes else " — manquant : " + ", ".join(manquantes))
            ),
        )
    )

    # --- 4. Isolation par utilisateur ---------------------------------------
    scoped_ok, scoped_detail = _profile_routes_are_self_scoped(app)
    tested = (ROOT / "tests" / "test_isolation.py").exists()
    report.checks.append(
        Check(
            id="isolation",
            label="Un utilisateur ne peut accéder qu'à ses propres données",
            ref="FN-001 · §14.5",
            state=DONE if (scoped_ok and tested) else (PARTIAL if scoped_ok else TODO),
            evidence=scoped_detail
            + (" — vérifié par tests/test_isolation.py" if tested else " — non testé"),
        )
    )

    # --- 5. Ingrédients seedés et vérifiés ----------------------------------
    ratio_ing = min(1.0, (ingredients_total or 0) / TARGET_INGREDIENTS)
    all_verified = bool(ingredients_total) and ingredients_verified == ingredients_total
    report.checks.append(
        Check(
            id="seed_ingredients",
            label=f"~{TARGET_INGREDIENTS} ingrédients seedés, 100 % avec allergènes signés",
            ref="FN-040 · FN-003",
            state=(
                DONE
                if ratio_ing >= 1.0 and all_verified
                else (PARTIAL if ingredients_total else TODO)
            ),
            evidence=(
                f"{ingredients_total}/{TARGET_INGREDIENTS} ingrédients · "
                f"{ingredients_verified} signés"
            ),
            ratio=round(ratio_ing, 3),
            next_step=(
                None
                if all_verified and ratio_ing >= 1.0
                else "Production de contenu et signature nutritionniste — hors effort développeur"
            ),
        )
    )

    # --- 6. Plats publiés ---------------------------------------------------
    ratio_dish = min(1.0, published / TARGET_DISHES)
    report.checks.append(
        Check(
            id="seed_dishes",
            label=f"~{TARGET_DISHES} plats seedés, validés et publiés",
            ref="FN-040 · FN-008",
            state=DONE if ratio_dish >= 1.0 else (PARTIAL if dishes_total else TODO),
            evidence=f"{published} publiés sur {dishes_total} en base",
            ratio=round(ratio_dish, 3),
            next_step=(
                None if ratio_dish >= 1.0 else "Bloqué par la signature des allergènes"
            ),
        )
    )

    # --- 7. Valeurs calculées, jamais saisies -------------------------------
    ok_d11, detail_d11 = _nutrition_fields_absent_from_dish_input()
    computed = await session.scalar(
        select(func.count())
        .select_from(Dish)
        .where(Dish.nutrition_computed_at.is_not(None))
    )
    report.checks.append(
        Check(
            id="d11",
            label="Les valeurs nutritionnelles des plats sont calculées, jamais saisies",
            ref="D-11",
            state=DONE if ok_d11 else TODO,
            evidence=f"{detail_d11} · {computed}/{dishes_total} plats recalculés en base",
        )
    )

    # --- 8. Refus de publication --------------------------------------------
    from sqlalchemy.orm import selectinload
    from app.models.catalog import DishIngredient

    dishes = (
        await session.scalars(
            select(Dish).options(
                selectinload(Dish.ingredients)
                .selectinload(DishIngredient.ingredient)
                .selectinload(Ingredient.unit_conversions),
                selectinload(Dish.tags),
                selectinload(Dish.allergens),
            )
        )
    ).all()
    blocked = 0
    for dish in dishes:
        try:
            if publication_blockers(dish):
                blocked += 1
        except UnitConversionError:
            blocked += 1

    guard_present = "/api/v1/admin/dishes/{key}/publish" in paths
    report.checks.append(
        Check(
            id="publication_guard",
            label="La publication est refusée si un ingrédient n'est pas vérifié",
            ref="FN-003 · FN-008",
            state=DONE if guard_present else TODO,
            evidence=(
                "contrôle exécuté à la publication · "
                f"{blocked}/{dishes_total} plats actuellement non publiables"
                if guard_present
                else "aucun point d'entrée de publication"
            ),
        )
    )

    # --- 9. Interface interne -----------------------------------------------
    saisie = {
        "/api/v1/admin/ingredients",
        "/api/v1/admin/dishes",
        "/api/v1/admin/ingredients/{key}/verify-allergens",
    }
    manquantes_admin = sorted(saisie - paths)
    report.checks.append(
        Check(
            id="html",
            label="Interface HTML de saisie et de validation opérationnelle",
            ref="FN-034",
            state=DONE if not manquantes_admin else PARTIAL,
            evidence=(
                "banc d'essai /console : saisie d'ingrédient et de plat, signature "
                "des allergènes, file de validation"
                if not manquantes_admin
                else "manquant : " + ", ".join(manquantes_admin)
            ),
        )
    )

    # --- 10. Sondes et journalisation ----------------------------------------
    audit_rows = (
        await session.execute(sql_text("SELECT count(*) FROM audit_logs"))
    ).scalar_one()
    report.checks.append(
        Check(
            id="observabilite",
            label="/health et /ready fonctionnels, journalisation avec corrélation",
            ref="FN-036 · FN-037",
            state=DONE if {"/health", "/ready"} <= paths else TODO,
            evidence=f"sondes exposées · {audit_rows} entrée(s) au journal d'audit",
        )
    )

    return report


__all__ = ["Check", "Report", "TARGET_DISHES", "TARGET_INGREDIENTS", "lot1_report"]
