"""Atomic liveness-heartbeat file helpers (shared by the scanner, the streaming runner, and
the ``polyarb healthcheck`` CLI).

A heartbeat file holds a single ``repr(float)`` epoch-seconds timestamp. The writer replaces it
atomically (write-then-rename) so a reader never sees a torn value, and write errors are swallowed
so a disk-full / permissions problem can never kill the loop that pulses it.

Two independent heartbeats exist:

- the **scan heartbeat** (D7) — pulsed once per scan-loop pass, proves the loop is not wedged;
- the **WS heartbeat** (R8) — pulsed by the streaming runner on each applied message *or*
  successful resync, proves the book cache is actually being kept fresh (not frozen).

The Docker HEALTHCHECK requires both fresh when streaming is the active path.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def now_epoch() -> float:
    """Current wall-clock epoch seconds (module-level so tests can monkeypatch it)."""
    return datetime.now(UTC).timestamp()


def write(path: Path | None, timestamp: float) -> None:
    """Atomically write ``timestamp`` to ``path`` (write-then-rename). No-op when ``path`` is None.

    All I/O errors are suppressed: a heartbeat write must never be able to crash the loop that
    emits it (consistent with the other best-effort I/O on the scan path).
    """
    if path is None:
        return
    with contextlib.suppress(Exception):
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False, suffix=".hb.tmp"
        ) as f:
            f.write(repr(timestamp))
            tmp_name = f.name
        os.replace(tmp_name, path)


def age(path: Path) -> float:
    """Seconds since the timestamp recorded in ``path``.

    Raises ``FileNotFoundError`` if absent or any ``Exception`` if the contents are unparseable —
    the healthcheck treats either as "not alive".
    """
    recorded = float(path.read_text().strip())
    return now_epoch() - recorded
