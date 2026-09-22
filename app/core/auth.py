"""Vérification des jetons ARISE (D-03, §4.3).

Le service ne détient **jamais** la clé privée de signature : il récupère la
clé publique via l'endpoint JWKS de NestJS, la met en cache avec un TTL, et
recharge le jeu de clés dès qu'un `kid` inconnu se présente — ce qui permet à
NestJS de faire tourner ses clés sans redéploiement ici.

Trois refus explicites, dans cet ordre :

1. **algorithme** — un jeton signé en HS256 est rejeté, même si sa signature
   est valide au regard d'un secret partagé. C'est la parade au piège n° 4 :
   accepter HS256 reviendrait à laisser ce service forger des jetons
   administrateur ARISE ;
2. **émetteur et audience** — `iss` doit être l'API ARISE, et `aud` doit
   désigner ce service ;
3. **droits** — l'accès aux fonctionnalités payantes est décidé par le claim
   `entitlements` (D-05), jamais recalculé ici.

Aucune donnée d'identité ARISE n'est conservée : seul `sub` est retenu, comme
`external_user_id` opaque (D-04).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.errors import ErrorCode, NutritionError

#: Seuls algorithmes acceptés. La liste est fermée : `jwt.decode` refuse tout
#: le reste, y compris `none` et la famille HMAC.
ALLOWED_ALGORITHMS = ["RS256"]

#: Droit requis pour accéder au module Nutrition (D-05).
NUTRITION_ENTITLEMENT = "nutrition"


class Role(StrEnum):
    """Rôles reconnus. `user` et `admin` viennent de NestJS ; `nutritionist`
    est propre au module Nutrition (§5.3) et sera ajouté côté ARISE."""

    USER = "user"
    ADMIN = "admin"
    NUTRITIONIST = "nutritionist"


@dataclass(frozen=True)
class Principal:
    """L'appelant, réduit à ce dont le service a besoin."""

    #: `sub` du jeton, stocké tel quel comme `external_user_id` (D-04).
    external_user_id: str
    role: str
    entitlements: tuple[str, ...] = ()
    issued_at: int | None = None
    expires_at: int | None = None
    #: Vrai lorsque le jeton a été vérifié avec la clé publique locale de
    #: développement plutôt que via JWKS. Jamais vrai en production.
    dev_key: bool = False

    def has_role(self, *roles: str) -> bool:
        return self.role in roles

    def has_entitlement(self, name: str) -> bool:
        return name in self.entitlements


def _unauthenticated(reason: str) -> NutritionError:
    """Le motif précis part au journal, pas au client : distinguer « signature
    invalide » de « audience incorrecte » aide surtout un attaquant."""
    return NutritionError(
        ErrorCode.UNAUTHENTICATED,
        http_status=status.HTTP_401_UNAUTHORIZED,
        details={"reason": reason},
    )


# --------------------------------------------------------------------------
# Jeu de clés publiques
# --------------------------------------------------------------------------


@dataclass
class JwksCache:
    """Cache du jeu de clés JWKS, avec rechargement sur `kid` inconnu.

    Le TTL évite un appel réseau par requête ; le rechargement forcé évite
    qu'une rotation de clé côté NestJS ne provoque une panne d'authentification
    jusqu'à expiration du cache.
    """

    url: str
    ttl_seconds: int
    _keys: dict[str, Any] = field(default_factory=dict)
    _fetched_at: float = 0.0
    #: Empêche un afflux de requêtes de déclencher autant de rechargements.
    _min_refresh_interval: float = 5.0
    _last_refresh_attempt: float = 0.0

    @property
    def _expired(self) -> bool:
        return (time.monotonic() - self._fetched_at) > self.ttl_seconds

    async def _fetch(self) -> None:
        self._last_refresh_attempt = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                document = response.json()
        except Exception as exc:  # noqa: BLE001
            raise NutritionError(
                ErrorCode.SERVICE_UNAVAILABLE,
                message="Le fournisseur d'identité est injoignable.",
                http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
                details={"jwks_url": self.url, "reason": type(exc).__name__},
            ) from exc

        keys: dict[str, Any] = {}
        for entry in jwt.PyJWKSet.from_dict(document).keys:
            if entry.key_id:
                keys[entry.key_id] = entry.key
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get(self, kid: str | None) -> Any:
        if self._expired or not self._keys:
            await self._fetch()

        if kid is None:
            # Jeu à clé unique : un `kid` absent reste exploitable.
            if len(self._keys) == 1:
                return next(iter(self._keys.values()))
            raise _unauthenticated("kid absent et jeu de clés multiple")

        if kid not in self._keys:
            # Rotation probable — un seul rechargement par fenêtre courte.
            if (time.monotonic() - self._last_refresh_attempt) > self._min_refresh_interval:
                await self._fetch()

        key = self._keys.get(kid)
        if key is None:
            raise _unauthenticated(f"kid inconnu : {kid}")
        return key

    def invalidate(self) -> None:
        self._keys = {}
        self._fetched_at = 0.0


_jwks = JwksCache(
    url=settings.jwt_jwks_url,
    ttl_seconds=settings.jwt_jwks_cache_ttl_seconds,
)


def _dev_public_key() -> Any | None:
    """Clé publique locale, pour développer avant la migration RS256 de NestJS.

    Refusée en production : `Settings` interdit déjà de la configurer là-bas,
    cette seconde barrière couvre le cas d'une variable injectée à chaud.
    """
    if settings.is_production or not settings.jwt_dev_public_key_path:
        return None
    path = Path(settings.jwt_dev_public_key_path)
    if not path.exists():
        return None
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    return load_pem_public_key(path.read_bytes())


# --------------------------------------------------------------------------
# Vérification
# --------------------------------------------------------------------------


async def decode_token(token: str) -> Principal:
    """Vérifie signature, expiration, `iss` et `aud`, puis réduit le jeton à un
    `Principal`."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _unauthenticated(f"en-tête illisible : {type(exc).__name__}") from exc

    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        # Refus explicite et prioritaire : c'est ce contrôle qui interdit
        # qu'un secret symétrique partagé serve à forger un jeton.
        raise _unauthenticated(f"algorithme refusé : {algorithm}")

    # Les jetons de la console n'ont pas de `kid` ; ceux de NestJS en portent
    # un. La clé locale ne sert donc qu'aux premiers : sans cette distinction,
    # configurer une clé de développement ferait refuser tout jeton réel.
    kid = header.get("kid")
    key = _dev_public_key() if kid is None else None
    used_dev_key = key is not None
    if key is None:
        key = await _jwks.get(kid)

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=ALLOWED_ALGORITHMS,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthenticated("jeton expiré") from exc
    except jwt.InvalidAudienceError as exc:
        raise _unauthenticated("audience incorrecte") from exc
    except jwt.InvalidIssuerError as exc:
        raise _unauthenticated("émetteur incorrect") from exc
    except jwt.PyJWTError as exc:
        raise _unauthenticated(f"jeton invalide : {type(exc).__name__}") from exc

    subject = claims.get("sub")
    if not subject or not isinstance(subject, str):
        raise _unauthenticated("sub absent ou non textuel")

    entitlements = claims.get("entitlements") or []
    if isinstance(entitlements, str):
        entitlements = [entitlements]

    return Principal(
        external_user_id=subject,
        role=str(claims.get("role") or Role.USER.value),
        entitlements=tuple(str(e) for e in entitlements),
        issued_at=claims.get("iat"),
        expires_at=claims.get("exp"),
        dev_key=used_dev_key,
    )


# --------------------------------------------------------------------------
# Dépendances FastAPI
# --------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False, description="Jeton ARISE (RS256)")


async def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Appelant authentifié. Toute route qui touche à des données utilisateur
    en dépend."""
    if credentials is None or not credentials.credentials:
        raise _unauthenticated("en-tête Authorization absent")

    principal = await decode_token(credentials.credentials)
    # Rendu disponible au journal d'audit sans repasser par la dépendance.
    request.state.principal = principal
    return principal


async def current_user(
    principal: Principal = Depends(current_principal),
) -> Principal:
    """Utilisateur disposant du droit `nutrition` (D-05).

    Les rôles internes traversent sans droit : un administrateur ou un
    nutritionniste n'a pas d'abonnement à souscrire pour valider un plat.
    """
    if principal.has_role(Role.ADMIN, Role.NUTRITIONIST):
        return principal
    if not principal.has_entitlement(NUTRITION_ENTITLEMENT):
        raise NutritionError(
            ErrorCode.FORBIDDEN,
            message="Votre abonnement ne comprend pas le module Nutrition.",
            http_status=status.HTTP_403_FORBIDDEN,
            details={"entitlements": list(principal.entitlements)},
        )
    return principal


def require_role(*roles: str):
    """Garde de rôle, transposée du `RolesGuard` NestJS."""

    allowed = tuple(str(r) for r in roles)

    async def _guard(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_role(*allowed):
            raise NutritionError(
                ErrorCode.FORBIDDEN,
                http_status=status.HTTP_403_FORBIDDEN,
                details={"required": list(allowed), "actual": principal.role},
            )
        return principal

    return _guard


#: Écriture du catalogue : administrateur ou nutritionniste (§5.2, §5.3).
require_catalog_editor = require_role(Role.ADMIN, Role.NUTRITIONIST)

#: Validation et publication d'un plat : nutritionniste uniquement (FN-008).
#: L'administrateur saisit, il ne signe pas.
require_validator = require_role(Role.NUTRITIONIST)

#: Plateforme de pilotage : lancer une collecte ou modifier un poids du score
#: engage le service entier. Un nutritionniste consulte, il ne règle pas.
#: S'ajoute à `require_catalog_editor`, que `tests/test_isolation.py` exige sur
#: toute route `/api/v1/admin`.
require_admin = require_role(Role.ADMIN)


__all__ = [
    "ALLOWED_ALGORITHMS",
    "JwksCache",
    "NUTRITION_ENTITLEMENT",
    "Principal",
    "Role",
    "current_principal",
    "current_user",
    "decode_token",
    "require_admin",
    "require_catalog_editor",
    "require_role",
    "require_validator",
]
