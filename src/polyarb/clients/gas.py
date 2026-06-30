"""Gas-cost oracle client — fetches live Polygon gas price and POL/USD rate.

Computes the USDC cost of on-chain execution from two public, keyless oracles:

- **Polygon Gas Station v2** (``gasstation.polygon.technology/v2``) → ``standard.maxFee``
  in gwei.
- **CoinGecko simple-price** → ``polygon-ecosystem-token`` / ``usd``.

Conversion (all Decimal, never float):
    ``gas_usdc(units) = Decimal(units) * gwei * Decimal("1e-9") * pol_usd``

where gwei→POL is the 1e-9 unit conversion and pol_usd translates POL to USDC.

Results are cached for ``ttl`` seconds (default 60 s) so a tight scan loop does not
hammer the free-tier CoinGecko API (~5-15 req/min). The cache holds the raw oracle
values ``(gwei, pol_usd)``; the two gas-unit estimates are derived on each call from
the module-level constants, so updating those constants takes effect immediately.

On any oracle failure (network error, non-200 HTTP status, missing/blank JSON keys,
unparseable number) the client raises :exc:`GasUnavailable` and does NOT crash the
caller. The integration layer catches that and falls back to static config values
(``Settings.gas_estimate`` / ``Settings.gas_per_leg_estimate``).

Context: on the Polymarket relayer path (proxy/Safe/deposit wallets) gas is covered by
Polymarket; these estimates apply for raw-EOA execution. See docs/API_NOTES.md §"Gas —
who pays, and our config defaults" for the full breakdown and the source of the
gas-unit constants.
"""

from __future__ import annotations

import time
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import httpx

# ---------------------------------------------------------------------------
# Public constants — module-level so integration code can reference/override them.
# ---------------------------------------------------------------------------

#: Polygon Gas Station v2.  Returns ``standard.maxFee`` in gwei.
GAS_STATION_URL: str = "https://gasstation.polygon.technology/v2"

#: CoinGecko simple-price base URL (query params are appended at request time).
COINGECKO_URL: str = "https://api.coingecko.com/api/v3/simple/price"

#: Gas units for one CTF merge/redeem or NegRisk ``mergePositions`` (single fixed tx).
#: Source: Polygonscan receipts (~125-192k observed; 250k is a conservative ceiling).
FIXED_GAS_UNITS: int = 250_000

#: Gas units for one exchange fill settlement (``matchOrders``).
#: Source: Polygonscan receipts (~380-670k observed; 700k is a conservative ceiling).
#: Multiply by leg count for multi-leg arbs.
PER_LEG_GAS_UNITS: int = 700_000

# CoinGecko query params (``matic-network`` id is deprecated → use the token id below).
_COINGECKO_PARAMS: dict[str, str] = {
    "ids": "polygon-ecosystem-token",
    "vs_currencies": "usd",
}

_DEFAULT_TIMEOUT = httpx.Timeout(10.0)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class GasUnavailable(Exception):
    """Either gas oracle is unreachable or returned unexpected / unparseable data.

    Callers should catch this and fall back to the static config values
    ``Settings.gas_estimate`` / ``Settings.gas_per_leg_estimate``.
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GasClient:
    """Fetches live Polygon gas price + POL/USD and returns USDC execution-cost estimates.

    Accepts an optional injected ``httpx.AsyncClient`` (used by tests via
    ``httpx.MockTransport``); when ``client`` is *None* a default client is created and
    owned (closed on :meth:`aclose`).

    The client does **not** extend :class:`~polyarb.clients.base.BaseHTTPClient` because
    it hits two unrelated external hosts rather than a single base URL.  Rate protection
    comes from the TTL cache rather than a token bucket — the two oracles are queried at
    most once every ``ttl`` seconds regardless of how often :meth:`gas_costs` is called.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        ttl: float = 60.0,
    ) -> None:
        """
        Args:
            client: Optional injected async HTTP client (for offline tests).
            ttl:    Cache lifetime in seconds (default 60 s).  Set to 0 to disable.
        """
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        self._owns_client = client is None
        self._ttl = ttl
        # Cache stores the raw oracle values; derived estimates are recomputed on access.
        self._cache: tuple[Decimal, Decimal] | None = None  # (gwei, pol_usd)
        self._cache_at: float = 0.0

    # ------------------------------------------------------------------
    # Internal fetchers
    # ------------------------------------------------------------------

    async def _fetch_gwei(self) -> Decimal:
        """Return ``standard.maxFee`` gwei from the Polygon Gas Station."""
        # Broad ``except Exception`` (not just httpx.HTTPError): the contract is "any oracle
        # failure → GasUnavailable" so the scan never aborts on gas. This also covers
        # httpx.InvalidURL (NOT an HTTPError) from a bad/overridden URL. CancelledError is a
        # BaseException, so cooperative cancellation still propagates.
        try:
            response = await self._client.get(GAS_STATION_URL)
            response.raise_for_status()
            data: Any = response.json()
            value = data["standard"]["maxFee"]
        except Exception as exc:
            raise GasUnavailable(f"gas station oracle failed: {exc}") from exc

        try:
            gwei = Decimal(str(value))
        except Exception as exc:
            raise GasUnavailable(f"unparseable gas price {value!r}: {exc}") from exc
        if gwei <= 0:
            # A broken oracle reporting 0/negative gwei would silently zero out gas and
            # overstate profit — reject so the caller falls back to the static estimate.
            raise GasUnavailable(f"non-positive gas price: {gwei}")
        return gwei

    async def _fetch_pol_usd(self) -> Decimal:
        """Return POL/USD from the CoinGecko simple-price endpoint."""
        # See _fetch_gwei: broad except so any oracle failure becomes GasUnavailable (fallback),
        # never an escaping exception that aborts the scan pass.
        try:
            response = await self._client.get(COINGECKO_URL, params=_COINGECKO_PARAMS)
            response.raise_for_status()
            data: Any = response.json()
            value = data["polygon-ecosystem-token"]["usd"]
        except Exception as exc:
            raise GasUnavailable(f"CoinGecko oracle failed: {exc}") from exc

        try:
            pol_usd = Decimal(str(value))
        except Exception as exc:
            raise GasUnavailable(f"unparseable POL/USD {value!r}: {exc}") from exc
        if pol_usd <= 0:
            raise GasUnavailable(f"non-positive POL/USD: {pol_usd}")
        return pol_usd

    # ------------------------------------------------------------------
    # Gas-unit → USDC conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _gas_usdc(units: int, gwei: Decimal, pol_usd: Decimal) -> Decimal:
        """Convert ``units`` gas units to USDC.

        ``gwei x 1e-9`` converts gwei to POL (the native Polygon token);
        ``x pol_usd`` converts POL to USDC.
        """
        return Decimal(units) * gwei * Decimal("1e-9") * pol_usd

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def gas_costs(self) -> tuple[Decimal, Decimal]:
        """Return ``(gas_estimate, gas_per_leg)`` USDC execution-cost estimates.

        ``gas_estimate``  — fixed cost of one CTF merge/redeem (``FIXED_GAS_UNITS``).
        ``gas_per_leg``   — per-leg fill-settlement cost (``PER_LEG_GAS_UNITS``).

        Results are cached for ``self._ttl`` seconds.  A second call within that window
        returns the cached values without hitting the network.

        Raises:
            GasUnavailable: If either oracle is unreachable or returns bad data.
                Callers should catch this and fall back to static config values.
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_at) < self._ttl:
            gwei, pol_usd = self._cache
        else:
            gwei = await self._fetch_gwei()
            pol_usd = await self._fetch_pol_usd()
            self._cache = (gwei, pol_usd)
            self._cache_at = time.monotonic()

        return (
            self._gas_usdc(FIXED_GAS_UNITS, gwei, pol_usd),
            self._gas_usdc(PER_LEG_GAS_UNITS, gwei, pol_usd),
        )

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created internally."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
