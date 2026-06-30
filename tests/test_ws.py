"""Offline tests for the MarketWebSocket client (clients/ws.py).

The dynamic-subscription select loop (R6) is the only non-trivial logic here; it is exercised by
monkeypatching ``websockets.connect`` with a fake connection so no network is touched.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import polyarb.clients.ws as ws_mod
from polyarb.clients.ws import MarketWebSocket


class _FakeConn:
    """Minimal stand-in for a websockets connection.

    ``_inbound`` is a list of raw JSON strings delivered by successive ``recv()`` calls; once
    exhausted, ``recv()`` blocks forever (simulating a live-but-quiet socket) unless ``closed`` is
    set, in which case it raises ConnectionClosed-like. ``sent`` records everything sent.
    """

    def __init__(self, inbound: list[str]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []
        self._closed = asyncio.Event()

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *_: object) -> None:
        self._closed.set()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self._inbound:
            return self._inbound.pop(0)
        await self._closed.wait()  # block until the connection is torn down
        raise AssertionError("recv after close")

    def __aiter__(self) -> _FakeConn:
        return self

    async def __anext__(self) -> str:
        if self._inbound:
            return self._inbound.pop(0)
        raise StopAsyncIteration


def _patch_connect(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    def fake_connect(url: str, **_: object) -> _FakeConn:
        return conn

    monkeypatch.setattr(
        ws_mod, "websockets", type("W", (), {"connect": staticmethod(fake_connect)})
    )


def test_initial_subscription_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a control queue, stream() sends the initial market subscription and yields msgs."""
    conn = _FakeConn([json.dumps({"event_type": "book", "asset_id": "t1"})])
    _patch_connect(monkeypatch, conn)

    async def go() -> list[dict[str, Any]]:
        out = []
        async for msg in MarketWebSocket().stream(["t1", "t2"]):
            out.append(msg)
        return out

    msgs = asyncio.run(go())
    assert msgs == [{"event_type": "book", "asset_id": "t1"}]
    assert conn.sent[0] == {"assets_ids": ["t1", "t2"], "type": "market"}


def test_control_ops_forwarded_to_live_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A control op queued mid-stream is sent on the live socket (R6 — no reconnect)."""
    conn = _FakeConn([json.dumps({"event_type": "book", "asset_id": "t1"})])
    _patch_connect(monkeypatch, conn)
    control: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def go() -> None:
        agen = MarketWebSocket().stream(["t1"], control=control)
        # First message arrives.
        first = await asyncio.wait_for(anext(agen), timeout=1.0)
        assert first["asset_id"] == "t1"
        # Queue a subscribe op; the next anext() (which would otherwise block on recv) must drain
        # the control queue and forward it, then keep waiting for messages.
        await control.put({"operation": "subscribe", "assets_ids": ["t2"]})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(agen), timeout=0.2)  # no more inbound msgs → blocks
        await agen.aclose()

    asyncio.run(go())
    # The initial subscription plus the forwarded dynamic subscribe.
    assert {"assets_ids": ["t1"], "type": "market"} in conn.sent
    assert {"operation": "subscribe", "assets_ids": ["t2"]} in conn.sent
