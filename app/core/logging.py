"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog
from structlog import stdlib as structlog_stdlib
from structlog import typing as structlog_typing

from app.core.config import settings


def setup_logging() -> None:
    """Configure structlog + stdlib logging."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # stdlib root
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # structlog processors
    processors: list[structlog_typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    if settings.is_dev:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.dict_tracebacks)
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog_stdlib.BoundLogger:
    """Get a structlog logger bound to a name."""
    return cast(structlog_stdlib.BoundLogger, structlog.get_logger(name))
