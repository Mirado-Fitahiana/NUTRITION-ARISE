"""Écriture du journal d'audit (FN-036).

Deux règles gouvernent ce module, et elles sont appliquées **ici** plutôt que
laissées à la discipline des appelants :

* **aucune donnée de santé en clair.** Un poids, une taille, une allergie ne
  figurent jamais dans `details` — seul le *fait* qu'ils ont changé y figure.
  `sanitize` filtre les clés interdites, quelle que soit leur origine ;
* **aucun secret.** Jetons, mots de passe et clés sont retirés de la même
  façon, sur le modèle du masquage déjà en place dans le middleware NestJS.

L'écriture est volontairement tolérante : un journal qui échoue ne doit pas
faire échouer l'opération métier qu'il observe. L'échec est lui-même journalisé,
côté application.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_correlation_id
from app.models.audit import AuditAction, AuditLog
from app.models.enums import AuditResult

logger = logging.getLogger("app.audit")

#: Clés dont la **valeur** ne doit jamais atteindre le journal. Les données de
#: santé (§4.6) côtoient ici les secrets : les deux sont exclues pour des motifs
#: différents, la règle d'écriture est la même.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        # Données de santé et d'identité
        "weight_kg", "height_cm", "birth_date", "sex", "age",
        "allergens", "allergies", "allergen", "restrictions",
        "declared_pregnancy", "declared_breastfeeding", "declared_medical_condition",
        "bmi", "imc", "city", "district", "latitude", "longitude",
        "email", "name", "phone",
        # Secrets
        "password", "token", "access_token", "refresh_token", "authorization",
        "secret", "api_key", "open_router_key", "prompt",
    }
)

#: Ce qui remplace une valeur filtrée. Explicite : une clé absente laisserait
#: croire que l'information n'existait pas.
REDACTED = "[masqué]"


def sanitize(details: dict[str, Any] | None) -> dict[str, Any]:
    """Retire récursivement les valeurs interdites, en conservant les clés.

    Conserver la clé est délibéré : savoir *que* le poids a changé est utile au
    débogage, connaître sa valeur ne l'est pas.
    """
    if not details:
        return {}

    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (REDACTED if k.lower() in FORBIDDEN_KEYS else _clean(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_clean(v) for v in value]
        return value

    return _clean(details)


async def record(
    session: AsyncSession,
    action: str,
    *,
    external_user_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    result: AuditResult = AuditResult.SUCCESS,
    details: dict[str, Any] | None = None,
) -> None:
    """Ajoute une entrée au journal, dans la session en cours.

    L'entrée est validée avec la transaction métier : un plat publié et un
    journal disant le contraire seraient pires que pas de journal du tout.
    """
    try:
        session.add(
            AuditLog(
                occurred_at=datetime.now(UTC),
                external_user_id=external_user_id,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                result=result,
                details=sanitize(details),
                correlation_id=get_correlation_id(),
            )
        )
    except Exception:  # noqa: BLE001
        # Un journal indisponible ne doit pas faire échouer l'opération.
        logger.exception("échec d'écriture du journal d'audit pour %s", action)


__all__ = ["FORBIDDEN_KEYS", "REDACTED", "AuditAction", "record", "sanitize"]
