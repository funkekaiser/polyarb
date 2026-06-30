"""Phase 0 smoke tests — prove the package imports and the CLI wiring resolves.

These run fully offline and hit no network. Later phases add detector/math/property tests.
"""

from __future__ import annotations

import polyarb
from polyarb.cli import app


def test_package_imports() -> None:
    # Alpha versioning: a non-empty PEP 440 string, kept in sync with pyproject.
    assert polyarb.__version__
    assert polyarb.__version__[0].isdigit()


def test_cli_app_exists() -> None:
    assert app is not None
