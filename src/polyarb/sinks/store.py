"""Persistence layer for detected opportunities.

``OpportunityStore`` is a typing.Protocol — swap in a Postgres implementation later by
satisfying the same interface. ``SqliteStore`` is the default: a single file (or ``:memory:``
for tests) with one row per opportunity, plus the full JSON payload for round-trip fidelity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from polyarb.models import Opportunity


def economic_fingerprint(opp: Opportunity) -> str:
    """Stable id for the distinct *economic event* an opportunity represents (E1 ledger).

    Re-detections of the same structural arb (same detector, same resolving markets, same leg
    structure) share a fingerprint, so the ledger tracks one tracked event rather than one row
    per scan pass. Granularity is deliberately ``detector + condition-set + leg-structure``:
    coarse enough to collapse 900 re-detections of one arb, fine enough that a genuinely
    different trade on the same markets is tracked separately. Size/price (which drift across
    detections) are excluded on purpose — resolution is a property of the markets, not the tick.
    """
    legs = sorted((leg.token_id, leg.side) for leg in opp.legs)
    parts = [
        str(opp.detector),
        *sorted(opp.condition_ids),
        *(f"{token_id}:{side}" for token_id, side in legs),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class LedgerEntry:
    """A distinct economic event in the ledger: its representative opp + resolution state."""

    fingerprint: str
    opp: Opportunity  # first-seen (representative) detection
    status: str  # pending | resolved | void | error
    detection_count: int
    first_detected_at: str
    last_detected_at: str
    realized_payoff: Decimal | None = None  # None until settled
    realized_pnl: Decimal | None = None  # None until settled


@runtime_checkable
class OpportunityStore(Protocol):
    """Persistence interface — Postgres or any other backend is a drop-in."""

    def record(self, opp: Opportunity) -> None:
        """Persist one opportunity (raw log) and upsert its economic event. Commits immediately."""
        ...

    def recent(self, limit: int = 100) -> list[Opportunity]:
        """Return the most-recently recorded opportunities, newest first."""
        ...

    def count(self) -> int:
        """Total number of persisted opportunities."""
        ...

    def distinct_events(self) -> int:
        """Total number of distinct economic events (deduped by fingerprint)."""
        ...

    def pending_events(self, limit: int = 500) -> list[LedgerEntry]:
        """Economic events awaiting resolution (status='pending'), oldest first."""
        ...

    def events(self, limit: int = 10000) -> list[LedgerEntry]:
        """All economic events, newest first, including realized outcomes."""
        ...

    def record_resolution(
        self,
        fingerprint: str,
        *,
        status: str,
        realized_payoff: Decimal | None,
        realized_pnl: Decimal | None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Write back a settled outcome for one economic event. Commits immediately."""
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

# E1 — the deduped realized-outcome ledger: one row per distinct economic event.
_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS economic_events (
    fingerprint       TEXT    PRIMARY KEY,
    detector          TEXT    NOT NULL,
    condition_ids     TEXT    NOT NULL,
    realizes          TEXT    NOT NULL,
    first_detected_at TEXT    NOT NULL,
    last_detected_at  TEXT    NOT NULL,
    detection_count   INTEGER NOT NULL,
    payload           TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending',
    resolved_at       TEXT,
    realized_payoff   REAL,
    realized_pnl      REAL,
    resolution_detail TEXT
)
"""

# First detection inserts; a re-detection only bumps the count + last-seen (first-seen economics
# and any recorded resolution are preserved).
_UPSERT_EVENT = """
INSERT INTO economic_events
    (fingerprint, detector, condition_ids, realizes,
     first_detected_at, last_detected_at, detection_count, payload, status)
VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'pending')
ON CONFLICT(fingerprint) DO UPDATE SET
    last_detected_at = excluded.last_detected_at,
    detection_count  = detection_count + 1
"""

_RESOLVE_EVENT = """
UPDATE economic_events
SET status = ?, resolved_at = ?, realized_payoff = ?, realized_pnl = ?, resolution_detail = ?
WHERE fingerprint = ?
"""

_EVENT_COLS = (
    "fingerprint, payload, status, detection_count, first_detected_at, "
    "last_detected_at, realized_payoff, realized_pnl"
)

_PENDING_EVENTS = f"""
SELECT {_EVENT_COLS}
FROM economic_events
WHERE status = 'pending'
ORDER BY first_detected_at ASC
LIMIT ?
"""

_ALL_EVENTS = f"""
SELECT {_EVENT_COLS}
FROM economic_events
ORDER BY first_detected_at DESC
LIMIT ?
"""

_RECENT = "SELECT payload FROM opportunities ORDER BY id DESC LIMIT ?"
_COUNT = "SELECT COUNT(*) FROM opportunities"
_COUNT_EVENTS = "SELECT COUNT(*) FROM economic_events"


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
        self._conn.execute(_CREATE_EVENTS)
        self._conn.commit()

    # -- OpportunityStore interface --

    def record(self, opp: Opportunity) -> None:
        """Log the raw detection AND upsert its economic event; one commit for both.

        detected_at is stamped as UTC ISO-8601. Re-detections of the same economic event
        (same ``economic_fingerprint``) collapse to one ``economic_events`` row.
        """
        detected_at = datetime.now(tz=UTC).isoformat()
        payload = opp.model_dump_json()
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
                payload,
            ),
        )
        self._conn.execute(
            _UPSERT_EVENT,
            (
                economic_fingerprint(opp),
                str(opp.detector),
                json.dumps(opp.condition_ids),
                opp.realizes,
                detected_at,
                detected_at,
                payload,
            ),
        )
        self._conn.commit()

    def distinct_events(self) -> int:
        """Total distinct economic events (deduped by fingerprint)."""
        row = self._conn.execute(_COUNT_EVENTS).fetchone()
        return int(row[0])

    @staticmethod
    def _row_to_entry(row: tuple[object, ...]) -> LedgerEntry:
        return LedgerEntry(
            fingerprint=str(row[0]),
            opp=Opportunity.model_validate_json(str(row[1])),
            status=str(row[2]),
            detection_count=int(str(row[3])),
            first_detected_at=str(row[4]),
            last_detected_at=str(row[5]),
            realized_payoff=None if row[6] is None else Decimal(str(row[6])),
            realized_pnl=None if row[7] is None else Decimal(str(row[7])),
        )

    def pending_events(self, limit: int = 500) -> list[LedgerEntry]:
        """Economic events still awaiting resolution, oldest first."""
        rows = self._conn.execute(_PENDING_EVENTS, (limit,)).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def events(self, limit: int = 10000) -> list[LedgerEntry]:
        """All economic events, newest first, including realized outcomes."""
        rows = self._conn.execute(_ALL_EVENTS, (limit,)).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def record_resolution(
        self,
        fingerprint: str,
        *,
        status: str,
        realized_payoff: Decimal | None,
        realized_pnl: Decimal | None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Write back a settled outcome for one economic event (read-only poller → ledger)."""
        self._conn.execute(
            _RESOLVE_EVENT,
            (
                status,
                datetime.now(tz=UTC).isoformat(),
                None if realized_payoff is None else float(realized_payoff),
                None if realized_pnl is None else float(realized_pnl),
                None if detail is None else json.dumps(detail),
                fingerprint,
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
