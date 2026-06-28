"""Data API client — public reads of trades/positions/holders.

Used later for resolution-risk and analytics signals. Returns raw JSON for now; typed
models can be added when the consuming code firms up. Public, no auth.
"""

from __future__ import annotations

from typing import Any, ClassVar

from polyarb.clients.base import BaseHTTPClient


class DataClient(BaseHTTPClient):
    service: ClassVar[str] = "data"
    base_url: ClassVar[str] = "https://data-api.polymarket.com"

    async def get_trades(
        self,
        *,
        market: str | None = None,
        limit: int = 100,
        **extra: Any,
    ) -> list[dict[str, Any]]:
        """Recent trades, optionally filtered to a market (conditionId)."""
        params: dict[str, Any] = {"limit": limit, **extra}
        if market is not None:
            params["market"] = market
        data = await self._get_json("/trades", endpoint="/trades", params=params)
        return list(data)
