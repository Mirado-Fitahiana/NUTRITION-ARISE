"""Socle des modèles SQLAlchemy."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Conventions de nommage : sans elles, Alembic génère des noms de contraintes
# anonymes que l'on ne peut plus cibler dans une migration ultérieure.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Type énuméré PostgreSQL utilisant les *valeurs* des membres.

    Par défaut, SQLAlchemy persiste le *nom* du membre (`PUBLISHED`). On veut
    les valeurs de la spécification (`published`) : ce sont elles qui figurent
    dans les contraintes `CHECK`, dans les payloads d'API et dans les fichiers
    de seed.
    """
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def enum_array_check(
    column: str, enum_cls: type[StrEnum], constraint_name: str
) -> CheckConstraint:
    """Contrainte de confinement d'une colonne tableau à un jeu de valeurs.

    PostgreSQL n'offre pas de type « tableau d'énumération » simple à faire
    évoluer. On utilise donc `text[]` borné par un `CHECK ... <@ ARRAY[...]`,
    ce qui garantit au niveau de la base qu'aucune valeur inconnue ne peut être
    écrite — exigence de FN-003 pour les allergènes.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(
        f"{column} <@ ARRAY[{values}]::text[]",
        name=constraint_name,
    )
