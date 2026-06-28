"""Capture live (read-only) Polymarket samples for inspection / as test fixtures.

Thin wrapper. The implementation lives in ``polyarb.recording`` so the CLI (``polyarb
record``) and this script share one code path. Run directly:

    uv run python scripts/record_fixtures.py [OUTPUT_DIR]
"""

from __future__ import annotations

import sys

from polyarb.recording import main

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
