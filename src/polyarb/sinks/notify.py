"""Alert notifiers — fire-and-forget; a failed alert never crashes the scan.

``Notifier`` is a typing.Protocol. ``NullNotifier`` is the silent default. ``WebhookNotifier``
POSTs the raw opportunity JSON to any HTTP endpoint (custom ingest). ``DiscordNotifier``
formats each opportunity into a Discord embed and POSTs it to a Discord incoming-webhook URL.
Use ``build_notifier`` to construct from a config string.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

from polyarb.models import Opportunity

log: structlog.BoundLogger = structlog.get_logger(__name__)


@runtime_checkable
class Notifier(Protocol):
    """Alert interface — async, fire-and-forget, never raises."""

    async def notify(self, opp: Opportunity) -> None:
        """Send an alert for a detected opportunity. Must not raise."""
        ...

    async def alert(self, title: str, body: str) -> None:
        """Send a generic text alert (e.g. the E2 settlement alarm). Must not raise."""
        ...

    async def aclose(self) -> None:
        """Release any resources (e.g. an owned HTTP client). Call on shutdown."""
        ...


class NullNotifier:
    """Silent notifier — the default when no alert target is configured."""

    async def notify(self, opp: Opportunity) -> None:
        pass

    async def alert(self, title: str, body: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


class WebhookNotifier:
    """Posts opportunities as JSON to an HTTP webhook.

    If no ``client`` is provided, an internal ``httpx.AsyncClient`` is created. Call
    ``aclose()`` to release it when done.

    Errors (HTTP non-2xx, network failures) are logged as warnings and swallowed so a
    flaky endpoint never disrupts the scan loop.
    """

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._own_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    async def notify(self, opp: Opportunity) -> None:
        """POST the opportunity payload; swallows all httpx errors."""
        try:
            resp = await self._client.post(self._url, json=opp.model_dump(mode="json"))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "webhook_http_error",
                url=self._url,
                status=exc.response.status_code,
            )
        except Exception as exc:
            # Protocol guarantee: notify() must never raise. httpx.InvalidURL (a malformed
            # NOTIFIER_URL) is NOT an httpx.HTTPError, so a narrower catch would let it escape
            # into the scanner's emit loop and wedge it. Swallow everything non-Cancel here.
            log.warning("webhook_error", url=self._url, error=str(exc))

    async def alert(self, title: str, body: str) -> None:
        """POST a generic {title, body} alert; swallows all errors."""
        try:
            resp = await self._client.post(self._url, json={"title": title, "body": body})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("webhook_http_error", url=self._url, status=exc.response.status_code)
        except Exception as exc:
            log.warning("webhook_error", url=self._url, error=str(exc))

    async def aclose(self) -> None:
        """Close the internal client (no-op when an external client was injected)."""
        if self._own_client:
            await self._client.aclose()


class DiscordNotifier:
    """Posts opportunities to a Discord channel via an incoming-webhook URL.

    Discord webhooks reject the raw Opportunity JSON that :class:`WebhookNotifier` sends —
    they require a payload carrying ``content`` and/or ``embeds`` — so this formats each
    opportunity into a compact, human-readable embed. Errors (non-2xx incl. 429 rate-limit,
    network failures, a malformed URL) are logged and swallowed: the ``Notifier`` protocol
    guarantees ``notify()`` never raises, so a flaky or rate-limited Discord webhook can
    never disrupt the scan loop.

    If no ``client`` is provided an internal ``httpx.AsyncClient`` is created; call
    ``aclose()`` to release it on shutdown.
    """

    # Discord embed accent colours (decimal RGB).
    _COLOR_INSTANT = 0x2ECC71  # green — realizes immediately (split/merge)
    _COLOR_HELD = 0x3498DB  # blue — held to resolution

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._own_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    def _payload(self, opp: Opportunity) -> dict[str, Any]:
        """Render an opportunity as a Discord webhook payload (one embed)."""
        color = self._COLOR_INSTANT if opp.realizes == "instant" else self._COLOR_HELD
        fields: list[dict[str, Any]] = [
            {"name": "Net", "value": f"{opp.net_profit_bps:.1f} bps", "inline": True},
            {"name": "Total net", "value": f"${opp.total_net_profit:.2f}", "inline": True},
            {"name": "Size", "value": f"{opp.executable_size}", "inline": True},
            {"name": "Risk", "value": str(opp.resolution_risk or "—"), "inline": True},
            {"name": "Realizes", "value": opp.realizes, "inline": True},
        ]
        if opp.days_to_resolution is not None:
            fields.append({"name": "Days", "value": str(opp.days_to_resolution), "inline": True})
        embed: dict[str, Any] = {
            # Discord caps: title 256, embed description 4096. Truncate defensively.
            "title": f"{opp.detector} · {opp.net_profit_bps:.1f} bps"[:256],
            "description": opp.description[:4096],
            "color": color,
            "fields": fields,
        }
        return {"embeds": [embed]}

    async def notify(self, opp: Opportunity) -> None:
        """POST a formatted embed; swallows all errors (incl. 429 rate-limit)."""
        try:
            resp = await self._client.post(self._url, json=self._payload(opp))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("discord_http_error", url=self._url, status=exc.response.status_code)
        except Exception as exc:
            # notify() must never raise (see WebhookNotifier): a malformed URL raises
            # httpx.InvalidURL, which is not an httpx.HTTPError. Swallow everything non-Cancel.
            log.warning("discord_error", url=self._url, error=str(exc))

    async def alert(self, title: str, body: str) -> None:
        """POST a plain-text alert as Discord `content`; swallows all errors."""
        content = f"**{title}**\n{body}"[:2000]  # Discord content cap
        try:
            resp = await self._client.post(self._url, json={"content": content})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("discord_http_error", url=self._url, status=exc.response.status_code)
        except Exception as exc:
            log.warning("discord_error", url=self._url, error=str(exc))

    async def aclose(self) -> None:
        """Close the internal client (no-op when an external client was injected)."""
        if self._own_client:
            await self._client.aclose()


def build_notifier(notifier: str, url: str | None = None) -> Notifier:
    """Construct a :class:`Notifier` from a config string.

    Parameters
    ----------
    notifier:
        ``"none"`` — silent; ``"webhook"`` — raw-JSON HTTP POST to ``url``;
        ``"discord"`` — formatted embed POST to a Discord incoming-webhook ``url``.
        Unknown values log a warning and fall back to ``NullNotifier``.
    url:
        Required when ``notifier`` is ``"webhook"`` or ``"discord"``; ignored otherwise.

    Raises
    ------
    ValueError
        When ``notifier`` is ``"webhook"``/``"discord"`` and ``url`` is None or empty.
    """
    if notifier == "none":
        return NullNotifier()
    if notifier == "webhook":
        if not url:
            raise ValueError("url is required for webhook notifier")
        return WebhookNotifier(url)
    if notifier == "discord":
        if not url:
            raise ValueError("url is required for discord notifier")
        return DiscordNotifier(url)
    # ntfy / telegram — not yet implemented
    log.warning("unknown_notifier", notifier=notifier, fallback="none")
    return NullNotifier()
