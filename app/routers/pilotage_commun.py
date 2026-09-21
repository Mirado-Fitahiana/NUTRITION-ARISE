"""Éléments partagés par les routeurs de la plateforme de pilotage.

`RoutePilotage` traduit une erreur de base de données en `503` **lisible**. Sans
elle, une base injoignable ou une migration non appliquée remonteraient comme un
`500` générique « Erreur interne du serveur » — vrai, mais inutile à l'opérateur
qui doit savoir quoi corriger. Le détail technique reste au journal (FN-037).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request, Response, status
from fastapi.routing import APIRoute
from sqlalchemy.exc import DBAPIError

from app.core.errors import ErrorCode, NutritionError
from app.services.supervision import motif_erreur

#: Connexion refusée, authentification échouée, table absente : toutes dérivent
#: de `DBAPIError`. Une autre exception reste un 500 générique, journalisé.
ERREURS_BASE = (DBAPIError,)


def base_indisponible(exc: BaseException) -> NutritionError:
    motif = motif_erreur(exc)
    return NutritionError(
        ErrorCode.SERVICE_UNAVAILABLE,
        message=motif[0].upper() + motif[1:] + ".",
        http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        details={"exception": type(exc).__name__},
    )


class RoutePilotage(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except ERREURS_BASE as exc:
                raise base_indisponible(exc) from exc

        return handler


def introuvable(message: str) -> NutritionError:
    return NutritionError(ErrorCode.NOT_FOUND, message=message, http_status=status.HTTP_404_NOT_FOUND)


def conflit(message: str) -> NutritionError:
    return NutritionError(ErrorCode.CONFLICT, message=message, http_status=status.HTTP_409_CONFLICT)


def invalide(message: str) -> NutritionError:
    return NutritionError(
        ErrorCode.VALIDATION_ERROR,
        message=message,
        http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


__all__ = ["RoutePilotage", "base_indisponible", "conflit", "introuvable", "invalide"]
