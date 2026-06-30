"""Application logging configuration.

Audit/SIEM events already have their own OWASP-shaped JSON payloads. This module
configures ordinary ``iceberg.*`` app logs around that path without changing the
``iceberg.audit`` stdout payload shape operators may already ingest.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

from .config import Settings

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "iceberg_correlation_id", default="-"
)
_MANAGED_HANDLER = "_iceberg_managed_handler"


def current_correlation_id() -> str:
    """Return the request correlation id for the current context, if any."""
    return _correlation_id.get()


def set_correlation_id(value: str) -> contextvars.Token[str]:
    """Set the request correlation id for logs emitted in this context."""
    return _correlation_id.set(value or "-")


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    """Restore the previous correlation id context."""
    _correlation_id.reset(token)


def effective_log_format(settings: Settings) -> str:
    """Resolve ``auto`` to a concrete log format."""
    if settings.log_format != "auto":
        return settings.log_format
    return "json" if settings.is_prod else "text"


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id()
        return True


class _AuditPassthroughMixin:
    def _audit_passthrough(self, record: logging.LogRecord) -> str | None:
        if record.name != "iceberg.audit":
            return None
        message = record.getMessage()
        try:
            json.loads(message)
        except json.JSONDecodeError:
            return None
        return message


class TextFormatter(_AuditPassthroughMixin, logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if passthrough := self._audit_passthrough(record):
            return passthrough
        return super().format(record)


class JsonFormatter(_AuditPassthroughMixin, logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if passthrough := self._audit_passthrough(record):
            return passthrough

        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Configure the ``iceberg`` logger family idempotently.

    Uvicorn's loggers remain server-owned; this installs one stdout handler on
    the app logger namespace and removes only handlers previously installed by
    this helper.
    """
    logger = logging.getLogger("iceberg")
    for handler in list(logger.handlers):
        if getattr(handler, _MANAGED_HANDLER, False):
            logger.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    setattr(handler, _MANAGED_HANDLER, True)
    handler.addFilter(CorrelationIdFilter())
    if effective_log_format(settings) == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter(
                "%(asctime)s %(levelname)s [%(name)s] [cid=%(correlation_id)s] %(message)s"
            )
        )

    logger.addHandler(handler)
    logger.setLevel(logging._nameToLevel[settings.log_level])  # noqa: SLF001 - validated setting
    logger.propagate = False
