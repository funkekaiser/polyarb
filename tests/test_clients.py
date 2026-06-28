"""Client tests — fully offline via httpx.MockTransport (no network, no live API)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import httpx

from polyarb.clients.clob import ClobClient
from polyarb.clients.gamma import GammaClient

FIXTURES = Path(__file__).parent / "fixtures"


def _transport(mapping: dict[str, str]) -> httpx.MockTransport:
    """Route requests to fixture files by a substring of the URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        for needle, fixture in mapping.items():
            if needle in request.url.path:
                return httpx.Response(200, text=(FIXTURES / fixture).read_text())
        return httpx.Response(404, text="{}")

    return httpx.MockTransport(handler)


def test_gamma_get_market_parses_offline() -> None:
    transport = _transport({"/markets/": "gamma_binary_market.json"})

    async def run() -> object:
        async with httpx.AsyncClient(transport=transport) as http:
            return await GammaClient(client=http).get_market("677396")

    market = asyncio.run(run())
    assert market.condition_id.startswith("0x")  # type: ignore[attr-defined]
    assert market.is_binary  # type: ignore[attr-defined]


def test_clob_order_book_parses_offline() -> None:
    transport = _transport({"/book": "clob_book_binary.json"})

    async def run() -> object:
        async with httpx.AsyncClient(transport=transport) as http:
            return await ClobClient(client=http).get_order_book("123")

    book = asyncio.run(run())
    assert book.best_ask is not None  # type: ignore[attr-defined]
    assert book.best_bid is not None  # type: ignore[attr-defined]
    assert book.best_ask.price < Decimal("1")  # type: ignore[attr-defined]


def test_clob_price_and_midpoint_offline() -> None:
    transport = _transport({"/price": "clob_price.json", "/midpoint": "clob_midpoint.json"})

    async def run() -> tuple[Decimal, Decimal]:
        async with httpx.AsyncClient(transport=transport) as http:
            client = ClobClient(client=http)
            return await client.get_price("123"), await client.get_midpoint("123")

    price, mid = asyncio.run(run())
    assert price == Decimal("0.063")
    assert mid == Decimal("0.0655")
