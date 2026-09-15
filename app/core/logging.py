"""Journalisation structurée et identifiant de corrélation (FN-036).

L'identifiant de corrélation est propagé depuis l'en-tête `X-Request-ID` émis
par le mobile ou par NestJS, afin qu'une même action soit traçable de bout en
bout. À défaut, il est généré ici.
"""

import logging
import sys
import uuid
from contextvars import ContextVar

CORRELATION_HEADER = "X-Request-ID"

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def set_correlation_id(value: str | None) -> str:
    correlation_id = value or str(uuid.uuid4())
    _correlation_id.set(correlation_id)
    return correlation_id


def get_correlation_id() -> str:
    return _correlation_id.get()


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(correlation_id)s] %(name)s — %(message)s"
        )
    )
    handler.addFilter(CorrelationFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
