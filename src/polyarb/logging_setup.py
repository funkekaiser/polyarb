"""structlog configuration.

JSON logs when stdout is not a TTY (containers, pipes — machine-readable for a log stack);
human-friendly console rendering when running interactively. Call ``configure_logging`` once
at process start (the CLI does this).
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=log_level)

    # httpx/httpcore log every request line at INFO ("HTTP Request: GET ... 404 Not Found").
    # During a scan that's hundreds of lines per pass — including the expected 404s for tokens
    # without a live CLOB book, which the scanner already skips — and it drowns our structured
    # logs. Keep them quiet at INFO/WARNING; honor the chosen level only when it's DEBUG (the
    # escape hatch — `max(level, WARNING)` would have pinned even DEBUG to WARNING).
    third_party_level = log_level if log_level <= logging.DEBUG else logging.WARNING
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(third_party_level)

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if sys.stdout.isatty()
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
