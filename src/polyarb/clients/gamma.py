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
