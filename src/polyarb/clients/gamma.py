"""Gamma API client — discovery of events and markets (public, no auth).

Base URL and quotas verified in docs/API_NOTES.md. Gamma metadata changes infrequently,
so callers should cache results rather than poll tightly.
"""

from __future__ import annotations

from typing import Any, ClassVar

from polyarb.clients.base import BaseHTTPClient
from polyarb.models import Event, Market


class GammaClient(BaseHTTPClient):
    service: ClassVar[str] = "gamma"
    base_url: ClassVar[str] = "https://gamma-api.polymarket.com"

    async def get_events(
        self,
        *,
        closed: bool = False,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
        **extra: Any,
    ) -> list[Event]:
        """List events. Defaults to open, active events (the scanner's candidates)."""
        params: dict[str, Any] = {
            "closed": str(closed).lower(),
            "active": str(active).lower(),
            "limit": limit,
            "offset": offset,
            **extra,
        }
        data = await self._get_json("/events", endpoint="/events", params=params)
        return [Event.model_validate(item) for item in data]

    async def get_event(self, event_id: str | int) -> Event:
        data = await self._get_json(f"/events/{event_id}", endpoint="/events")
        return Event.model_validate(data)

    async def get_markets(
        self,
        *,
        closed: bool = False,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
        **extra: Any,
    ) -> list[Market]:
        params: dict[str, Any] = {
            "closed": str(closed).lower(),
            "active": str(active).lower(),
            "limit": limit,
            "offset": offset,
            **extra,
        }
        data = await self._get_json("/markets", endpoint="/markets", params=params)
        return [Market.model_validate(item) for item in data]

    async def get_market(self, market_id: str | int) -> Market:
        data = await self._get_json(f"/markets/{market_id}", endpoint="/markets")
        return Market.model_validate(data)

    async def resolved_markets(self, condition_ids: list[str]) -> list[Market]:
        """Fetch CLOSED (resolved) markets for the given condition ids — read-only (E1 settle).

        Filters ``/markets`` by ``closed=true`` + ``condition_ids`` (a resolved market is not
        ``active``, so the active filter is dropped). NOTE: the ``condition_ids`` filter is not
        yet live-verified in this repo — see docs/API_NOTES.md; the first live ``polyarb settle``
        run should confirm resolutions come back. All settlement logic is fully offline-tested
        against a fake resolver independent of this query.
        """
        if not condition_ids:
            return []
        return await self.get_markets(
            closed=True,
            active=False,
            limit=max(len(condition_ids), 100),
            condition_ids=condition_ids,
        )
