"""Configuration du service Nutrition.

Toute la configuration transite par l'environnement (12-factor). Les valeurs
sensibles sont typées `SecretStr` afin de ne jamais apparaître dans un log ou
dans une trace d'exception.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # --- Base de données ---
    database_host: str = "localhost"
    database_port: int = 5432
    database_user: str = "postgres"
    database_password: SecretStr = SecretStr("")
    database_name: str = "arise_nutrition"

    # --- Authentification (D-03) ---
    jwt_jwks_url: str = "http://localhost:3000/.well-known/jwks.json"
    jwt_issuer: str = "arise-api"
    jwt_audience: str = "arise-nutrition"
    jwt_jwks_cache_ttl_seconds: int = 3600
    # Clé publique locale, pour développer sans attendre la migration RS256 de
    # NestJS. Doit rester vide en production.
    jwt_dev_public_key_path: str = ""
    # Clé privée locale, uniquement pour émettre des jetons de test depuis le
    # banc d'essai. D-03 : le service ne doit jamais détenir de clé de
    # signature en production — la contrainte ci-dessous le garantit.
    jwt_dev_private_key_path: str = ""
    jwt_dev_token_ttl_seconds: int = 7200

    # --- Fournisseur IA (lot 2) ---
    open_router_key: SecretStr = SecretStr("")
    llm_model: str = "openai/gpt-4o-mini"
    llm_timeout_seconds: int = 30

    # --- Observabilité ---
    sentry_dsn: str = ""

    @computed_field
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _no_signing_key_in_production(self) -> "Settings":
        """D-03 — le service ne détient jamais de clé de signature en
        production. La contrainte est vérifiée au démarrage : un déploiement
        mal configuré doit refuser de démarrer, pas fonctionner discrètement.
        """
        if self.environment == "production" and (
            self.jwt_dev_private_key_path or self.jwt_dev_public_key_path
        ):
            raise ValueError(
                "JWT_DEV_PUBLIC_KEY_PATH et JWT_DEV_PRIVATE_KEY_PATH doivent être "
                "vides en production : la clé publique vient de JWKS, et le "
                "service ne signe jamais de jeton (D-03)."
            )
        return self

    def _dsn(self, driver: str) -> str:
        password = self.database_password.get_secret_value()
        return (
            f"postgresql+{driver}://{self.database_user}:{password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def database_url_async(self) -> str:
        """DSN utilisé par l'application (SQLAlchemy asyncio + psycopg 3)."""
        return self._dsn("psycopg")

    @property
    def database_url_sync(self) -> str:
        """DSN utilisé par Alembic, qui exécute ses migrations en synchrone."""
        return self._dsn("psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
