# ARISE Nutrition Service — configuration locale. Ne pas versionner.

ENVIRONMENT=development
LOG_LEVEL=INFO
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_USER=postgres
DATABASE_PASSWORD=root
DATABASE_NAME=arise_nutrition
JWT_JWKS_URL=http://localhost:3000/.well-known/jwks.json
JWT_ISSUER=arise-api
JWT_AUDIENCE=arise-nutrition
JWT_JWKS_CACHE_TTL_SECONDS=3600
JWT_DEV_PUBLIC_KEY_PATH=keys/dev_jwt_public.pem
JWT_DEV_PRIVATE_KEY_PATH=keys/dev_jwt_private.pem
JWT_DEV_TOKEN_TTL_SECONDS=7200
OPEN_ROUTER_KEY=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
LLM_MODEL=openai/gpt-4o-mini
LLM_TIMEOUT_SECONDS=30
SENTRY_DSN=
