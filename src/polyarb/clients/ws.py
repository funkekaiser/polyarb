"""CLOB market websocket — live order book / price updates (public, no auth).

Subscribe to many token_ids on a SINGLE connection (the docs publish no concurrent-
connection cap, but fanning out connections is wasteful and risky — prefer one). The market
channel is public; the user channel (auth) is deliberately not implemented.

Dynamic (re)subscription (R6)
-----------------------------
``stream`` accepts an optional ``control`` queue of operation messages
(``{"operation": "subscribe"|"unsubscribe", "assets_ids": [...]}``, API_NOTES §WS). When
supplied, the generator races inbound market messages against the control queue and forwards
queued ops to the live connection — so the engine can add/drop tokens WITHOUT dropping the feed
(no reconnect). With ``control=None`` (the default / tests) it is a plain message generator.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import websockets

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketWebSocket:
    """Streams market-channel messages for a set of token_ids."""

    def __init__(self, url: str = MARKET_WS_URL, *, ping_interval: float = 10.0) -> None:
        self._url = url
        self._ping_interval = ping_interval

    async def stream(
        self,
        token_ids: Sequence[str],
        *,
        control: asyncio.Queue[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield decoded messages for ``token_ids`` until the connection closes.

        Reconnection/backoff is layered by the engine (``StreamingBooks``); this is the raw
        stream. When ``control`` is provided, ops drained from it are sent on the live socket
        (dynamic subscribe/unsubscribe, R6) interleaved with message delivery.
        """
        subscribe = {"assets_ids": list(token_ids), "type": "market"}
        async with websockets.connect(self._url, ping_interval=self._ping_interval) as conn:
            await conn.send(json.dumps(subscribe))
            if control is None:
                async for raw in conn:
                    yield json.loads(raw)
                return

            # Race inbound messages against control ops on one connection. websockets permits a
            # concurrent send while a recv is in flight, so forwarding ops never blocks delivery.
            recv_task: asyncio.Task[Any] = asyncio.ensure_future(conn.recv())
            ctrl_task: asyncio.Task[dict[str, Any]] = asyncio.ensure_future(control.get())
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {recv_task, ctrl_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if ctrl_task in done:
                        op = ctrl_task.result()
                        await conn.send(json.dumps(op))
                        ctrl_task = asyncio.ensure_future(control.get())
                    if recv_task in done:
                        try:
                            raw = recv_task.result()
                        except websockets.exceptions.ConnectionClosedOK:
                            return  # clean server-initiated close → end iteration normally
                        recv_task = asyncio.ensure_future(conn.recv())
                        yield json.loads(raw)
            finally:
                # Cancel both pending tasks on any exit (close, error, generator .aclose()).
                for task in (recv_task, ctrl_task):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
