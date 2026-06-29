"""Tests for logging configuration — the noisy-third-party level hatch."""

from __future__ import annotations

import logging

from polyarb.logging_setup import configure_logging


def test_third_party_level_debug_hatch() -> None:
    # httpx/httpcore are pinned to WARNING at INFO (they'd drown the structured logs), but the
    # chosen level is honored at DEBUG (the escape hatch). Restore the level so the global
    # logging state isn't left mutated for the rest of the suite.
    httpx_logger = logging.getLogger("httpx")
    saved = httpx_logger.level
    try:
        configure_logging("DEBUG")
        assert httpx_logger.level == logging.DEBUG  # honored

        configure_logging("INFO")
        assert httpx_logger.level == logging.WARNING  # pinned (would be INFO without the hatch)
    finally:
        httpx_logger.setLevel(saved)
