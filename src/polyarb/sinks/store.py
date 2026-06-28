"""Persistence layer for detected opportunities.

``OpportunityStore`` is a typing.Protocol — swap in a Postgres implementation later by
satisfying the same interface. ``SqliteStore`` is the default: a single file (or ``:memory:``
for tests) with one row per opportunity, plus the full JSON payload for round-trip fidelity.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from polyarb.models import Opportunity


@runtime_checkable
class OpportunityStore(Protocol):
    """Persistence interface — Postgres or any other backend is a drop-in."""

    def record(self, opp: Opportunity) -> None:
        """Persist one opportunity. Commits immediately."""
        ...

    def recent(self, limit: int = 100) -> list[Opportunity]:
        """Return the most-recently recorded opportunities, newest first."""
        ...

    def count(self) -> int:
        """Total number of persisted opportunities."""
        ...

    def close(self) -> None:
        """Release the underlying connection."""
        ...


_CREATE = """
CREATE TABLE IF NOT EXISTS opportunities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT    NOT NULL,
    detector        TEXT    NOT NULL,
    event_id        TEXT,
    net_profit_bps  REAL    NOT NULL,
    net_profit      REAL    NOT NULL,
    executable_size REAL    NOT NULL,
    realizes        TEXT    NOT NULL,
    resolution_risk TEXT,
    payload         TEXT    NOT NULL
)
"""

_INSERT = """
INSERT INTO opportunities
    (detected_at, detector, event_id, net_profit_bps, net_profit,
     executable_size, realizes, resolution_risk, payload)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_RECENT = "SELECT payload FROM opportunities ORDER BY id DESC LIMIT ?"
_COUNT = "SELECT COUNT(*) FROM opportunities"


class SqliteStore:
    """SQLite-backed opportunity store. Thread-safety: single-threaded use only.

    Parameters
    ----------
    path:
        File path for the database, or ``:memory:`` (default) for ephemeral storage.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(_CREATE)
        self._conn.commit()

    # -- OpportunityStore interface --

    def record(self, opp: Opportunity) -> None:
        """Insert one row; detected_at is stamped as UTC ISO-8601."""
        detected_at = datetime.now(tz=UTC).isoformat()
        self._conn.execute(
            _INSERT,
            (
                detected_at,
                str(opp.detector),
                opp.event_id,
                float(opp.net_profit_bps),
                float(opp.net_profit),
                float(opp.executable_size),
                opp.realizes,
                opp.resolution_risk,
                opp.model_dump_json(),
            ),
        )
        self._conn.commit()

    def recent(self, limit: int = 100) -> list[Opportunity]:
        """Return up to ``limit`` opportunities, newest first, as full model objects."""
        rows = self._conn.execute(_RECENT, (limit,)).fetchall()
        return [Opportunity.model_validate_json(row[0]) for row in rows]

    def count(self) -> int:
        """Total rows in the opportunities table."""
        row = self._conn.execute(_COUNT).fetchone()
        return int(row[0])

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # -- context manager --

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
