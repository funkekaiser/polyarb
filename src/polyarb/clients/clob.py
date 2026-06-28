"""CLOB API client — PUBLIC READS ONLY (order books, prices, midpoints).

Hard rule (SPEC constraint #2): this client never trades. It exposes only the public,
no-auth read endpoints (`/book`, `/price`, `/midpoint`) and holds no signing key.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal

from polyarb.clients.base import BaseHTTPClient
from polyarb.models import OrderBook

Side = Literal["buy", "sell"]


class ClobClient(BaseHTTPClient):
    service: ClassVar[str] = "clob"
    base_url: ClassVar[str] = "https://clob.polymarket.com"

    async def get_order_book(self, token_id: str) -> OrderBook:
        data = await self._get_json("/book", endpoint="/book", params={"token_id": token_id})
        return OrderBook.model_validate(data)

    async def get_price(self, token_id: str, side: Side = "buy") -> Decimal:
        """Best price for a side: ``buy`` returns the best bid, ``sell`` the best ask."""
        data = await self._get_json(
            "/price", endpoint="/price", params={"token_id": token_id, "side": side}
        )
        return Decimal(str(data["price"]))

    async def get_midpoint(self, token_id: str) -> Decimal:
        data = await self._get_json(
            "/midpoint", endpoint="/midpoint", params={"token_id": token_id}
        )
        return Decimal(str(data["mid"]))
