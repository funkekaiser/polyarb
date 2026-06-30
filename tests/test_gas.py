"""Tests for GasClient — fully offline via httpx.MockTransport.

All assertions use Decimal arithmetic; no live network is touched.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from polyarb.clients.gas import (
    COINGECKO_URL,
    FIXED_GAS_UNITS,
    GAS_STATION_URL,
    PER_LEG_GAS_UNITS,
    GasClient,
    GasUnavailable,
)

# ---------------------------------------------------------------------------
# Fixture oracle values
# ---------------------------------------------------------------------------

_GAS_MAXFEE: float = 30.0  # gwei  (standard.maxFee)
_POL_USD: float = 0.10  # USD/POL

_GAS_DEFAULT_BODY = {"standard": {"maxFee": _GAS_MAXFEE, "maxPriorityFee": 25.0}}
_CG_DEFAULT_BODY = {"polygon-ecosystem-token": {"usd": _POL_USD}}


# ---------------------------------------------------------------------------
# Transport factory
# ---------------------------------------------------------------------------


def _make_transport(
    *,
    gas_status: int = 200,
    gas_body: object | None = None,
    cg_status: int = 200,
    cg_body: object | None = None,
) -> tuple[httpx.MockTransport, list[int]]:
    """Return ``(transport, counter)`` where ``counter[0]`` is the total request count.

    Routes by hostname: ``gasstation.polygon.technology`` → gas station;
    ``coingecko.com`` → CoinGecko price.  Any other host returns 404.
    """
    counter = [0]

    _gas = gas_body if gas_body is not None else _GAS_DEFAULT_BODY
    _cg = cg_body if cg_body is not None else _CG_DEFAULT_BODY

    def handler(request: httpx.Request) -> httpx.Response:
        counter[0] += 1
        host = request.url.host
        if "gasstation.polygon.technology" in host:
            return httpx.Response(gas_status, json=_gas)
        if "coingecko.com" in host:
            return httpx.Response(cg_status, json=_cg)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), counter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected(units: int) -> Decimal:
    """Recompute the expected USDC cost using the same formula as the client."""
    return Decimal(units) * Decimal(str(_GAS_MAXFEE)) * Decimal("1e-9") * Decimal(str(_POL_USD))


# ---------------------------------------------------------------------------
# Computation correctness
# ---------------------------------------------------------------------------


def test_gas_costs_returns_exact_decimal_values() -> None:
    """gas_costs() returns exact Decimal values matching the conversion formula.

    Given gas station ``standard.maxFee = 30`` gwei and CoinGecko ``usd = 0.10``:
      gas_estimate  = FIXED_GAS_UNITS    * 30 * 1e-9 * 0.10
      gas_per_leg   = PER_LEG_GAS_UNITS  * 30 * 1e-9 * 0.10
    Expected values are recomputed in the test (no hardcoded rounded literal).
    """
    transport, _ = _make_transport()

    async def go() -> tuple[Decimal, Decimal]:
        async with httpx.AsyncClient(transport=transport) as http:
            return await GasClient(client=http).gas_costs()

    estimate, per_leg = asyncio.run(go())

    assert estimate == _expected(FIXED_GAS_UNITS)
    assert per_leg == _expected(PER_LEG_GAS_UNITS)


def test_gas_costs_both_positive() -> None:
    """Sanity: both estimates must be strictly positive for the fixture values."""
    transport, _ = _make_transport()

    async def go() -> tuple[Decimal, Decimal]:
        async with httpx.AsyncClient(transport=transport) as http:
            return await GasClient(client=http).gas_costs()

    estimate, per_leg = asyncio.run(go())
    assert estimate > Decimal(0)
    assert per_leg > Decimal(0)
    # per-leg covers more gas units → must cost more
    assert per_leg > estimate


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_two_calls_within_ttl_fetch_network_once() -> None:
    """Two gas_costs() calls within the TTL window make exactly 2 HTTP requests
    (one to the gas station, one to CoinGecko) rather than 4.
    """
    transport, counter = _make_transport()

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            client = GasClient(client=http, ttl=60.0)
            await client.gas_costs()  # fetches both oracles
            await client.gas_costs()  # must use cache — no new requests

    asyncio.run(go())
    assert counter[0] == 2  # 1 gas station + 1 CoinGecko; second call hits cache


def test_call_after_ttl_expiry_refetches() -> None:
    """With ttl=0 the cache expires immediately, so every call re-fetches both oracles."""
    transport, counter = _make_transport()

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            client = GasClient(client=http, ttl=0.0)
            await client.gas_costs()  # fetch 1
            await client.gas_costs()  # fetch 2 (cache expired)

    asyncio.run(go())
    assert counter[0] == 4  # 2 endpoints x 2 fetches


def test_cached_values_are_numerically_identical() -> None:
    """The cached result must equal the first fetch — not re-derived with drift."""
    transport, _ = _make_transport()

    async def go() -> tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]:
        async with httpx.AsyncClient(transport=transport) as http:
            client = GasClient(client=http, ttl=60.0)
            first = await client.gas_costs()
            second = await client.gas_costs()
            return first, second

    first, second = asyncio.run(go())
    assert first == second


# ---------------------------------------------------------------------------
# Graceful failure — gas station
# ---------------------------------------------------------------------------


def test_gas_station_http_500_raises_gas_unavailable() -> None:
    """HTTP 500 from the gas station raises GasUnavailable (not a raw httpx error)."""
    transport, _ = _make_transport(gas_status=500)

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_gas_station_missing_standard_key_raises_gas_unavailable() -> None:
    """Gas station returning {} (no 'standard' key) raises GasUnavailable."""
    transport, _ = _make_transport(gas_body={})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_gas_station_null_maxfee_raises_gas_unavailable() -> None:
    """Gas station returning maxFee=null (None) raises GasUnavailable."""
    transport, _ = _make_transport(gas_body={"standard": {"maxFee": None}})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_gas_station_non_numeric_maxfee_raises_gas_unavailable() -> None:
    """Gas station returning maxFee='n/a' (non-numeric string) raises GasUnavailable."""
    transport, _ = _make_transport(gas_body={"standard": {"maxFee": "n/a"}})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_gas_station_zero_maxfee_raises_gas_unavailable() -> None:
    """maxFee=0 must NOT silently produce zero gas (which would overstate profit) — a broken
    oracle reporting non-positive gas raises GasUnavailable so the caller falls back."""
    transport, _ = _make_transport(gas_body={"standard": {"maxFee": 0}})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_gas_station_negative_maxfee_raises_gas_unavailable() -> None:
    """A negative gas price is nonsensical → GasUnavailable, never a negative gas cost."""
    transport, _ = _make_transport(gas_body={"standard": {"maxFee": -5.0}})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Graceful failure — CoinGecko
# ---------------------------------------------------------------------------


def test_coingecko_http_500_raises_gas_unavailable() -> None:
    """HTTP 500 from CoinGecko raises GasUnavailable."""
    transport, _ = _make_transport(cg_status=500)

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_coingecko_empty_body_raises_gas_unavailable() -> None:
    """CoinGecko returning {} (empty dict / wrong asset id) raises GasUnavailable."""
    transport, _ = _make_transport(cg_body={})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_coingecko_missing_usd_key_raises_gas_unavailable() -> None:
    """CoinGecko response missing the 'usd' sub-key raises GasUnavailable."""
    transport, _ = _make_transport(cg_body={"polygon-ecosystem-token": {}})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_coingecko_null_usd_raises_gas_unavailable() -> None:
    """CoinGecko returning usd=null (None) raises GasUnavailable."""
    transport, _ = _make_transport(cg_body={"polygon-ecosystem-token": {"usd": None}})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


def test_coingecko_zero_usd_raises_gas_unavailable() -> None:
    """usd=0 must not silently zero out gas → GasUnavailable (forces static fallback)."""
    transport, _ = _make_transport(cg_body={"polygon-ecosystem-token": {"usd": 0}})

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(GasUnavailable):
                await GasClient(client=http).gas_costs()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Exception type / API shape
# ---------------------------------------------------------------------------


def test_gas_unavailable_is_subclass_of_exception() -> None:
    """GasUnavailable must be catchable as a plain Exception for fallback handlers."""
    assert issubclass(GasUnavailable, Exception)


def test_gas_unavailable_carries_message() -> None:
    """GasUnavailable's message includes enough context for a log line."""
    transport, _ = _make_transport(gas_body={})

    async def go() -> str:
        async with httpx.AsyncClient(transport=transport) as http:
            try:
                await GasClient(client=http).gas_costs()
            except GasUnavailable as exc:
                return str(exc)
        return ""  # unreachable

    msg = asyncio.run(go())
    assert msg  # non-empty — something describable went wrong


# ---------------------------------------------------------------------------
# Async context manager
# ---------------------------------------------------------------------------


def test_async_context_manager_works() -> None:
    """GasClient can be used as ``async with GasClient(...) as gc:``."""
    transport, _ = _make_transport()

    async def go() -> tuple[Decimal, Decimal]:
        async with (
            httpx.AsyncClient(transport=transport) as http,
            GasClient(client=http) as gc,
        ):
            return await gc.gas_costs()

    estimate, per_leg = asyncio.run(go())
    assert estimate == _expected(FIXED_GAS_UNITS)
    assert per_leg == _expected(PER_LEG_GAS_UNITS)


def test_injected_client_is_not_closed_by_gas_client() -> None:
    """When a client is injected (not owned), aclose() must NOT close it.

    The owner (e.g. the scanner) manages the lifetime of its shared client.
    """
    transport, _ = _make_transport()

    async def go() -> bool:
        http = httpx.AsyncClient(transport=transport)
        gc = GasClient(client=http)
        await gc.gas_costs()
        await gc.aclose()
        # The injected client should still be alive; a subsequent request should work.
        try:
            await GasClient(client=http).gas_costs()
            return True
        except Exception:
            return False
        finally:
            await http.aclose()

    assert asyncio.run(go()) is True


# ---------------------------------------------------------------------------
# URL constants are importable (integration smoke-check)
# ---------------------------------------------------------------------------


def test_url_constants_are_non_empty_strings() -> None:
    """GAS_STATION_URL and COINGECKO_URL must be non-empty str for overridability."""
    assert isinstance(GAS_STATION_URL, str) and GAS_STATION_URL
    assert isinstance(COINGECKO_URL, str) and COINGECKO_URL


def test_gas_unit_constants_are_positive_integers() -> None:
    """Gas-unit constants must be positive int (used in Decimal(units) conversion)."""
    assert isinstance(FIXED_GAS_UNITS, int) and FIXED_GAS_UNITS > 0
    assert isinstance(PER_LEG_GAS_UNITS, int) and PER_LEG_GAS_UNITS > 0
    assert PER_LEG_GAS_UNITS > FIXED_GAS_UNITS  # per-leg is the larger of the two
