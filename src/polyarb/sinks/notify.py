"""Alert notifiers — fire-and-forget; a failed alert never crashes the scan.

``Notifier`` is a typing.Protocol. ``NullNotifier`` is the silent default. ``WebhookNotifier``
POSTs the opportunity JSON to any HTTP endpoint (useful for ntfy, Discord webhooks, or
custom ingest). Use ``build_notifier`` to construct from a config string.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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

    async def aclose(self) -> None:
        """Release any resources (e.g. an owned HTTP client). Call on shutdown."""
        ...


class NullNotifier:
    """Silent notifier — the default when no alert target is configured."""

    async def notify(self, opp: Opportunity) -> None:
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
        except httpx.HTTPError as exc:
            log.warning("webhook_transport_error", url=self._url, error=str(exc))

    async def aclose(self) -> None:
        """Close the internal client (no-op when an external client was injected)."""
        if self._own_client:
            await self._client.aclose()


def build_notifier(notifier: str, url: str | None = None) -> Notifier:
    """Construct a :class:`Notifier` from a config string.

    Parameters
    ----------
    notifier:
        ``"none"`` — silent; ``"webhook"`` — HTTP POST to ``url``.
        Unknown values log a warning and fall back to ``NullNotifier``.
    url:
        Required when ``notifier="webhook"``; ignored otherwise.

    Raises
    ------
    ValueError
        When ``notifier="webhook"`` and ``url`` is None or empty.
    """
    if notifier == "none":
        return NullNotifier()
    if notifier == "webhook":
        if not url:
            raise ValueError("url is required for webhook notifier")
        return WebhookNotifier(url)
    # ntfy / discord / telegram — not yet implemented
    log.warning("unknown_notifier", notifier=notifier, fallback="none")
    return NullNotifier()
