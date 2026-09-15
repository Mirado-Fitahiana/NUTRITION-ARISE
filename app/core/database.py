"""Accès à la base PostgreSQL du service Nutrition.

Le service possède sa propre base (§4.2) : aucune clé étrangère ne pointe vers
la base ARISE, et aucun modèle ne référence une table NestJS.
"""

from collections.abc import AsyncIterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine = create_async_engine(
    settings.database_url_async,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionFactory = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dépendance FastAPI : une session par requête, annulée en cas d'erreur."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database() -> bool:
    """Interroge réellement la base — utilisé par la sonde `/ready` (FN-037)."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# --- Accès synchrone ---
# Réservé aux scripts hors serveur (seed, tâches de maintenance). Il évite la
# contrainte de boucle d'événements que psycopg impose sous Windows, et une
# commande en ligne n'a rien à gagner à être asynchrone.
def create_sync_session_factory() -> sessionmaker[Session]:
    sync_engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return sessionmaker(bind=sync_engine, expire_on_commit=False, autoflush=False)
