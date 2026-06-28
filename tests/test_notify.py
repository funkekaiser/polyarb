"""Offline tests for notifiers — no network; uses httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from polyarb.models import DetectorKind, Opportunity
from polyarb.sinks.notify import NullNotifier, WebhookNotifier, build_notifier

ZERO = Decimal(0)


def _opp() -> Opportunity:
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="test",
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal("0.90"),
        gross_profit=Decimal("0.10"),
        fees=ZERO,
        gas=ZERO,
        net_profit=Decimal("0.10"),
        net_profit_bps=Decimal("1111"),
        executable_size=Decimal("100"),
        realizes="instant",
    )


# ---------------------------------------------------------------------------
# NullNotifier
# ---------------------------------------------------------------------------


def test_null_notifier_is_noop() -> None:
    """NullNotifier.notify returns without error and has no side effects."""
    notifier = NullNotifier()
    asyncio.run(notifier.notify(_opp()))  # must not raise


# ---------------------------------------------------------------------------
# WebhookNotifier — happy path
# ---------------------------------------------------------------------------


def test_webhook_posts_json_with_expected_fields() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier("https://example.com/hook", client=client)
    opp = _opp()

    asyncio.run(notifier.notify(opp))

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    body = json.loads(req.content)
    assert body["detector"] == "complement"
    assert "net_profit_bps" in body


# ---------------------------------------------------------------------------
# WebhookNotifier — error resilience
# ---------------------------------------------------------------------------


def test_webhook_swallows_http_500() -> None:
    """A 500 response must not propagate out of notify()."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier("https://example.com/hook", client=client)
    asyncio.run(notifier.notify(_opp()))  # must not raise


def test_webhook_swallows_transport_error() -> None:
    """A transport-level error must not propagate out of notify()."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier("https://example.com/hook", client=client)
    asyncio.run(notifier.notify(_opp()))  # must not raise


# ---------------------------------------------------------------------------
# build_notifier
# ---------------------------------------------------------------------------


def test_build_notifier_none_returns_null() -> None:
    n = build_notifier("none")
    assert isinstance(n, NullNotifier)


def test_build_notifier_webhook_returns_webhook() -> None:
    n = build_notifier("webhook", url="https://example.com/hook")
    assert isinstance(n, WebhookNotifier)


def test_build_notifier_webhook_no_url_raises() -> None:
    with pytest.raises(ValueError, match="url"):
        build_notifier("webhook", url=None)


def test_build_notifier_webhook_empty_url_raises() -> None:
    with pytest.raises(ValueError, match="url"):
        build_notifier("webhook", url="")


def test_build_notifier_unknown_returns_null() -> None:
    # "discord" is not yet implemented — should warn and return NullNotifier
    n = build_notifier("discord", url="https://discord.com/api/webhooks/...")
    assert isinstance(n, NullNotifier)
