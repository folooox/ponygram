"""
Structured async-compatible logging for ponygram.

Uses structlog with:
- Coloured console output in development (log_level == DEBUG)
- JSON console output in production
- Rotating file handler (10 MB per file, 5 backups)
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog


def _get_python_log_level(level_name: str) -> int:
    """Convert a level name string to the stdlib logging integer constant."""
    return getattr(logging, level_name.upper(), logging.INFO)


def setup_logging(log_level: str = "INFO", log_file: str = "logs/ponygram.log") -> None:
    """
    Configure structlog and the stdlib logging machinery.

    Call once at application startup before any loggers are created.

    Parameters
    ----------
    log_level:
        One of DEBUG / INFO / WARNING / ERROR / CRITICAL.
    log_file:
        Path to the rotating log file.  Parent directories are created
        automatically.
    """
    numeric_level = _get_python_log_level(log_level)
    is_debug = numeric_level <= logging.DEBUG

    # ------------------------------------------------------------------ #
    # stdlib root logger                                                   #
    # ------------------------------------------------------------------ #
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove any handlers that may have been added by imported libraries
    root_logger.handlers.clear()

    # ------------------------------------------------------------------ #
    # Console handler                                                      #
    # ------------------------------------------------------------------ #
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    root_logger.addHandler(console_handler)

    # ------------------------------------------------------------------ #
    # Rotating file handler                                                #
    # ------------------------------------------------------------------ #
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    root_logger.addHandler(file_handler)

    # ------------------------------------------------------------------ #
    # Silence noisy third-party loggers                                   #
    # ------------------------------------------------------------------ #
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ------------------------------------------------------------------ #
    # structlog shared processors                                          #
    # ------------------------------------------------------------------ #
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_debug:
        # Human-friendly coloured output for development
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # Machine-parseable JSON for production / log aggregators
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    for handler in root_logger.handlers:
        handler.setFormatter(formatter)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Return a structlog bound logger with *name* as the module context key.

    Usage::

        log = get_logger(__name__)
        await log.ainfo("message", key="value")
        log.info("sync message", key="value")
    """
    return structlog.get_logger(name)
