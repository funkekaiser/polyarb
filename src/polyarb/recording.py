"""Capture live (read-only) Polymarket samples for inspection and as test fixtures.

Run via the CLI: ``uv run polyarb record`` (see polyarb.cli). This is the ONLY routinely-run
code that touches the live API, and it is strictly read-only — Gamma + CLOB public reads.
It writes parsed-model JSON, which both proves the typed clients work end-to-end against
live data and gives fresh fixtures to refresh tests from.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import structlog

from polyarb.clients.clob import ClobClient
from polyarb.clients.gamma import GammaClient
from polyarb.models import Event, Market

log = structlog.get_logger("polyarb.record")


def _dump(path: Path, obj: Any) -> Path:
    path.write_text(json.dumps(obj, indent=2, default=str))
    return path


def _binary_markets(events: list[Event]) -> list[Market]:
    """Tradeable binary markets across the discovered events (candidates for a book fetch)."""
    return [
        m for e in events for m in e.markets if m.is_binary and not m.neg_risk and m.clob_token_ids
    ]


async def record(output_dir: Path, *, limit: int = 50) -> list[Path]:
    """Pull a representative live sample into ``output_dir``; return the files written."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    async with GammaClient() as gamma, ClobClient() as clob:
        events = await gamma.get_events(closed=False, active=True, limit=limit)
        log.info("fetched_events", count=len(events))
        written.append(
            _dump(output_dir / "events.json", [e.model_dump(mode="json") for e in events])
        )

        negrisk = next((e for e in events if e.is_multi_outcome), None)
        if negrisk is not None:
            written.append(
                _dump(output_dir / "negrisk_event.json", negrisk.model_dump(mode="json"))
            )
            log.info(
                "recorded_negrisk_event", title=negrisk.title.strip(), outcomes=len(negrisk.markets)
            )

        # Not every discovered market has a live CLOB book (some 404). Try candidates until
        # one returns a book.
        recorded_book = False
        for binary in _binary_markets(events):
            try:
                book = await clob.get_order_book(binary.yes_token_id)
            except httpx.HTTPStatusError as exc:
                log.debug(
                    "no_book_for_market", question=binary.question, status=exc.response.status_code
                )
                continue
            written.append(_dump(output_dir / "binary_market.json", binary.model_dump(mode="json")))
            written.append(_dump(output_dir / "order_book.json", book.model_dump(mode="json")))
            log.info(
                "recorded_binary_market",
                question=binary.question,
                fee_free=binary.is_fee_free,
                best_bid=str(book.best_bid.price) if book.best_bid else None,
                best_ask=str(book.best_ask.price) if book.best_ask else None,
            )
            recorded_book = True
            break
        if not recorded_book:
            log.warning("no_binary_market_with_book_found")

    log.info("record_complete", files=len(written), output_dir=str(output_dir))
    return written


def main(output_dir: str | None = None) -> None:
    target = Path(output_dir) if output_dir else Path("tests/fixtures/recorded")
    asyncio.run(record(target))


if __name__ == "__main__":
    main()
