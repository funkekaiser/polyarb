"""CLOB market websocket — live order book / price updates (public, no auth).

Subscribe to many token_ids on a SINGLE connection (the docs publish no concurrent-
connection cap, but fanning out connections is wasteful and risky — prefer one). The market
channel is public; the user channel (auth) is deliberately not implemented.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import websockets

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketWebSocket:
    """Streams market-channel messages for a set of token_ids."""

    def __init__(self, url: str = MARKET_WS_URL, *, ping_interval: float = 10.0) -> None:
        self._url = url
        self._ping_interval = ping_interval

    async def stream(self, token_ids: Sequence[str]) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded messages for ``token_ids`` until the connection closes.

        Reconnection/backoff is layered by the engine (Phase 3); this is the raw stream.
        """
        subscribe = {"assets_ids": list(token_ids), "type": "market"}
        async with websockets.connect(self._url, ping_interval=self._ping_interval) as conn:
            await conn.send(json.dumps(subscribe))
            async for raw in conn:
                yield json.loads(raw)
