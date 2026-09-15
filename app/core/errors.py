"""Erreurs fonctionnelles et enveloppe de réponse (FN-037).

L'enveloppe reprend **champ pour champ** celle du filtre global NestJS
(`global-exception.filter.ts`) : `statusCode`, `timestamp`, `path`, `method`,
`message`, `error`, `requestId`. Le mobile n'a ainsi qu'une seule logique de
traitement d'erreur, quel que soit le backend interrogé.

Deux champs propres au service Nutrition s'y ajoutent :

* `code` — code d'erreur **stable et documenté**, sur lequel le client peut
  brancher un comportement ;
* `recoveryAction` — l'action de reprise proposée à l'utilisateur, quand elle
  existe.

Aucun détail technique n'est exposé : il est journalisé.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_correlation_id


class ErrorCode(StrEnum):
    """Codes fonctionnels de FN-037, plus les codes transverses.

    Ces chaînes font partie du contrat d'API : elles ne changent jamais sans
    version d'API.
    """

    # --- Fonctionnels (FN-037) ---
    PROFILE_INCOMPLETE = "PROFILE_INCOMPLETE"
    NO_COMPATIBLE_DISH = "NO_COMPATIBLE_DISH"
    CATALOG_TOO_SMALL = "CATALOG_TOO_SMALL"
    BUDGET_IMPOSSIBLE = "BUDGET_IMPOSSIBLE"
    MEDICAL_LIMIT = "MEDICAL_LIMIT"
    PERIOD_INVALID = "PERIOD_INVALID"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"

    # --- Transverses ---
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # --- Catalogue (FN-003 / FN-008) ---
    INGREDIENT_NOT_VERIFIED = "INGREDIENT_NOT_VERIFIED"
    DISH_NOT_PUBLISHABLE = "DISH_NOT_PUBLISHABLE"


#: Messages en français, compréhensibles par l'utilisateur final.
DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.PROFILE_INCOMPLETE: "Votre profil est incomplet pour générer un programme.",
    ErrorCode.NO_COMPATIBLE_DISH: (
        "Aucun plat ne correspond à vos contraintes pour ce repas."
    ),
    ErrorCode.CATALOG_TOO_SMALL: (
        "Le catalogue ne permet pas encore de couvrir cette période."
    ),
    ErrorCode.BUDGET_IMPOSSIBLE: (
        "Le budget indiqué ne permet pas d'atteindre vos besoins nutritionnels."
    ),
    ErrorCode.MEDICAL_LIMIT: (
        "Votre situation nécessite l'avis d'un professionnel de santé. "
        "Nous ne pouvons pas générer de programme."
    ),
    ErrorCode.PERIOD_INVALID: "La période demandée est invalide.",
    ErrorCode.PRICE_UNAVAILABLE: "Aucun prix connu pour certains ingrédients.",
    ErrorCode.QUOTA_EXCEEDED: "Vous avez atteint votre quota de générations.",
    ErrorCode.VALIDATION_ERROR: "Les données envoyées sont invalides.",
    ErrorCode.UNAUTHENTICATED: "Authentification requise.",
    ErrorCode.FORBIDDEN: "Accès refusé.",
    ErrorCode.NOT_FOUND: "Ressource introuvable.",
    ErrorCode.CONFLICT: "L'opération entre en conflit avec l'état actuel.",
    ErrorCode.INTERNAL_ERROR: "Erreur interne du serveur.",
    ErrorCode.SERVICE_UNAVAILABLE: "Service temporairement indisponible.",
    ErrorCode.INGREDIENT_NOT_VERIFIED: (
        "Les allergènes de cet ingrédient ne sont pas vérifiés."
    ),
    ErrorCode.DISH_NOT_PUBLISHABLE: "Ce plat ne remplit pas les conditions de publication.",
}

#: Action de reprise proposée, quand elle existe (FN-037).
RECOVERY_ACTIONS: dict[ErrorCode, str] = {
    ErrorCode.PROFILE_INCOMPLETE: "complete_profile",
    # Assouplir une préférence — jamais une allergie.
    ErrorCode.NO_COMPATIBLE_DISH: "relax_preference",
    ErrorCode.CATALOG_TOO_SMALL: "shorten_period",
    ErrorCode.BUDGET_IMPOSSIBLE: "raise_budget",
    ErrorCode.MEDICAL_LIMIT: "consult_professional",
    ErrorCode.PERIOD_INVALID: "fix_dates",
    ErrorCode.QUOTA_EXCEEDED: "wait_quota_reset",
}


class NutritionError(Exception):
    """Erreur fonctionnelle portant un code stable.

    `details` n'est **jamais** renvoyé au client : il alimente le journal.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | None = None,
        recovery_action: str | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGES[code]
        self.http_status = http_status
        self.details = details or {}
        self.recovery_action = recovery_action or RECOVERY_ACTIONS.get(code)
        super().__init__(self.message)


def build_error_body(
    request: Request,
    http_status: int,
    code: ErrorCode,
    message: str,
    recovery_action: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "statusCode": http_status,
        "timestamp": datetime.now(UTC).isoformat(),
        "path": request.url.path,
        "method": request.method,
        "message": message,
        "error": code.value,
        "code": code.value,
        "requestId": get_correlation_id(),
    }
    if recovery_action:
        body["recoveryAction"] = recovery_action
    return body


def register_exception_handlers(app) -> None:
    """Branche les gestionnaires. Toute erreur non prévue devient un 500
    générique : le détail est journalisé, jamais exposé."""

    import logging

    logger = logging.getLogger("app.errors")

    @app.exception_handler(NutritionError)
    async def _nutrition_error(request: Request, exc: NutritionError) -> JSONResponse:
        logger.warning(
            "erreur fonctionnelle %s sur %s — %s",
            exc.code.value,
            request.url.path,
            exc.details or "sans détail",
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=build_error_body(
                request, exc.http_status, exc.code, exc.message, exc.recovery_action
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=build_error_body(
                request,
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                ErrorCode.VALIDATION_ERROR,
                DEFAULT_MESSAGES[ErrorCode.VALIDATION_ERROR],
            )
            | {"fields": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = {
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.CONFLICT,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = exc.detail if isinstance(exc.detail, str) else DEFAULT_MESSAGES[code]
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_body(request, exc.status_code, code, message),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("exception non gérée sur %s", request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=build_error_body(
                request,
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                ErrorCode.INTERNAL_ERROR,
                DEFAULT_MESSAGES[ErrorCode.INTERNAL_ERROR],
            ),
        )


__all__ = [
    "DEFAULT_MESSAGES",
    "ErrorCode",
    "NutritionError",
    "build_error_body",
    "register_exception_handlers",
]
