"""Regression tests for bugs found in the adversarial bug-hunt (see docs/TESTING.md)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polyarb.detectors.base import ZERO, Profit, Snapshot, make_opportunity
from polyarb.detectors.dependency import DependencyDetector
from polyarb.detectors.negrisk_basket import NegRiskBasketDetector
from polyarb.engine.scanner import _days_to_resolution
from polyarb.models import DetectorKind, Market, OrderBook
from polyarb.pricing.fees import taker_fee
from polyarb.resolution.relations import (
    Comparator,
    ComparatorKind,
    MarketTags,
    Relation,
    add_relation,
    generate_ladder_relations,
)
from polyarb.sinks.notify import NullNotifier, build_notifier
from tests.helpers import make_book, make_event, make_market

ONE = Decimal(1)


# Bug 1 — naive end_date must not raise (would poison a whole scan pass).
def test_naive_end_date_does_not_crash_days_helper() -> None:
    m = Market.model_validate(
        {
            "id": "1",
            "conditionId": "0xC",
            "question": "q",
            "clobTokenIds": '["Y","N"]',
            "endDate": "2099-01-01T00:00:00",
        }  # no Z → naive datetime
    )
    assert m.end_date is not None and m.end_date.tzinfo is None
    days = _days_to_resolution([m], datetime.now(UTC))
    assert days["0xC"] > 0


# Bug 2 — taker_fee must never be negative for out-of-range prices.
def test_taker_fee_never_negative() -> None:
    assert taker_fee(Decimal("1.1"), ONE, Decimal("0.07")) == 0
    assert taker_fee(Decimal("-0.1"), ONE, Decimal("0.07")) == 0
    assert taker_fee(Decimal("0.5"), ONE, Decimal("0.07")) > 0  # valid range still charged


# Bug 3 — a today-resolving (days=0) arb gets a real annualized, not None.
def _profit() -> Profit:
    return Profit(cost=Decimal("0.90"), gross_profit=Decimal("0.10"), fees=ZERO)


def test_days_zero_annualizes_high_not_none() -> None:
    opp0 = make_opportunity(
        detector=DetectorKind.NEGRISK_BASKET,
        description="d",
        condition_ids=["0x1"],
        legs=[],
        profit=_profit(),
        executable_size=ONE,
        realizes="resolution",
        days_by_condition={"0x1": 0},
    )
    assert opp0.annualized is not None
    # days floored to 1 → (0.10/0.90) * 365
    assert opp0.annualized == (Decimal("0.10") / Decimal("0.90")) * Decimal(365)


def test_days_none_leaves_annualized_none() -> None:
    opp = make_opportunity(
        detector=DetectorKind.NEGRISK_BASKET,
        description="d",
        condition_ids=["0x1"],
        legs=[],
        profit=_profit(),
        executable_size=ONE,
        realizes="resolution",
        days_by_condition=None,
    )
    assert opp.annualized is None


# Bug 4 — OrderBook coerces a fractional float timestamp.
def test_order_book_coerces_float_timestamp() -> None:
    book = OrderBook(market="0xc", asset_id="t", timestamp_ms=1234567890.9, bids=[], asks=[])
    assert book.timestamp_ms == 1234567890


# Bug 5 — non-binary markets must not IndexError.
def test_yes_outcome_guards_non_binary() -> None:
    m = Market.model_validate(
        {"id": "1", "conditionId": "0x1", "question": "q", "clobTokenIds": "[]"}
    )
    assert not m.is_binary
    with pytest.raises(ValueError):
        m.yes_outcome()


def test_event_outcomes_skips_non_binary() -> None:
    non_binary = Market.model_validate(
        {"id": "1", "conditionId": "0xNB", "question": "q", "clobTokenIds": "[]"}
    )
    binary = make_market("0xB", yes="y", no="n")
    event = make_event([non_binary, binary])
    outs = event.outcomes()
    assert [o.condition_id for o in outs] == ["0xB"]


# Bug 6 — notifier lifecycle: aclose exists and is safe.
def test_notifier_aclose() -> None:
    async def run() -> None:
        await NullNotifier().aclose()
        webhook = build_notifier("webhook", "https://example.com/hook")
        await webhook.aclose()  # must not raise; closes the owned client

    asyncio.run(run())


# Bug 7 — a self-loop relation must not emit (and add_relation rejects it).
def test_dependency_skips_self_loop() -> None:
    m = make_market("c1", yes="y", no="n")
    snap = Snapshot(
        markets=[m],
        relations=[Relation("c1", "c1", "self")],
        books={
            "y": make_book("y", asks=[("0.40", "10")]),
            "n": make_book("n", asks=[("0.40", "10")]),
        },
    )
    assert list(DependencyDetector().detect(snap)) == []


def test_add_relation_rejects_self_loop() -> None:
    with pytest.raises(ValueError):
        add_relation("x", "x", "loop")


# Bug 8 — ladder must not emit a relation between two equal-bound markets.
def _tag(cid: str, bound: str) -> MarketTags:
    return MarketTags(
        cid, "ETH", Comparator.THRESHOLD_GTE, bound, ComparatorKind.CUMULATIVE_TOUCH, "fp"
    )


def test_ladder_skips_equal_bounds() -> None:
    rels = generate_ladder_relations([_tag("a", "8000"), _tag("b", "8000"), _tag("c", "10000")])
    pairs = {(r.antecedent_condition_id, r.consequent_condition_id) for r in rels}
    assert ("a", "b") not in pairs and ("b", "a") not in pairs
    # only the 10000 ⇒ 8000 rung (higher ⇒ lower) survives
    assert len(rels) == 1


# ── Second adversarial bug-hunt (2026-06-29) ──────────────────────────────────


# Bug 9 / D3 — dependency horizon is max(legs): capital is locked until the LATER leg resolves.
# (This supersedes the old "use B's horizon" rule — A, the antecedent, can resolve after B, e.g.
# a sports nesting where make-playoffs (B) settles before win-championship (A). Discriminating:
# B=10, A=100 → D3 gives 100; the old B-with-fallback gave 10.)
def test_dependency_horizon_is_max_of_legs() -> None:
    a = make_market("0xA", yes="yA", no="nA", fee_type="crypto_fees_v2")
    b = make_market("0xB", yes="yB", no="nB", fee_type="crypto_fees_v2")
    snap = Snapshot(
        markets=[a, b],
        relations=[Relation("0xA", "0xB", "A ⇒ B")],
        books={
            "nA": make_book("nA", asks=[("0.30", "50")]),
            "yB": make_book("yB", asks=[("0.30", "80")]),
        },
        days_to_resolution={"0xB": 10, "0xA": 100},  # A resolves later → max is 100, not B's 10
    )
    opp = next(iter(DependencyDetector().detect(snap)))
    assert opp.days_to_resolution == 100  # max(10, 100), not B's 10


# Bug 10 — a malformed (non-httpx) book error must not kill the whole scan pass.
def test_fetch_books_survives_non_http_error() -> None:
    from polyarb.config import Settings
    from polyarb.engine.scanner import Scanner

    class _FakeClob:
        async def get_order_book(self, token_id: str) -> OrderBook:
            if token_id == "bad":
                raise ValueError("malformed CLOB payload")  # not an httpx.HTTPError
            return make_book(token_id, asks=[("0.40", "10")])

    scanner = Scanner(Settings(), gamma=None, clob=_FakeClob(), store=None)  # type: ignore[arg-type]

    async def run() -> dict[str, OrderBook]:
        return await scanner._fetch_books({"bad", "good"})

    books = asyncio.run(run())
    assert set(books) == {"good"}  # bad token skipped, pass survives


# Bug 11 — webhook notify must swallow httpx.InvalidURL (not an httpx.HTTPError).
def test_webhook_notify_swallows_invalid_url() -> None:
    import httpx

    from polyarb.sinks.notify import WebhookNotifier

    class _RaisingClient:
        async def post(self, *args: object, **kwargs: object) -> object:
            raise httpx.InvalidURL("malformed url")

        async def aclose(self) -> None:
            pass

    opp = make_opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="d",
        condition_ids=["0x1"],
        legs=[],
        profit=_profit(),
        executable_size=ONE,
        realizes="instant",
    )
    notifier = WebhookNotifier("not-a-url", client=_RaisingClient())  # type: ignore[arg-type]
    asyncio.run(notifier.notify(opp))  # must not raise


# Bug 12 — a fractional timestamp delivered as a string must coerce, not raise.
def test_order_book_coerces_string_float_timestamp() -> None:
    book = OrderBook(market="0xc", asset_id="t", timestamp_ms="1700000000.9", bids=[], asks=[])
    assert book.timestamp_ms == 1700000000


# Bug 13 — a zero-size level must not become the best quote (phantom size-0 opp / missed arb).
def test_zero_size_levels_skipped_in_best_quote() -> None:
    book = make_book(
        "t",
        asks=[("0.40", "0"), ("0.41", "100")],
        bids=[("0.39", "0"), ("0.30", "100")],
    )
    assert book.best_ask is not None and book.best_ask.price == Decimal("0.41")
    assert book.best_ask.size == Decimal("100")
    assert book.best_bid is not None and book.best_bid.price == Decimal("0.30")


# Bug 14 — "geopolitics" fee type must not match the "politics" → ELEVATED substring.
def test_geopolitics_not_classified_elevated() -> None:
    from polyarb.resolution.risk import ResolutionRisk, classify_market

    geo = make_market("0xG").model_copy(update={"fee_type": "geopolitics_fees"})
    assert classify_market(geo) == ResolutionRisk.STANDARD
    pol = make_market("0xP").model_copy(update={"fee_type": "politics_fees"})
    assert classify_market(pol) == ResolutionRisk.ELEVATED


# ── Backlog B2 — gas modeled per-execution, not per-set ──────────────────────────────────


def test_b2_gas_per_execution_not_per_set() -> None:
    """Gas is a fixed per-execution cost; the same per-set edge can be positive or negative
    depending on trade size.

    At small size the gas overwhelms the per-set net and total_net_profit < 0.
    At large size the per-set edge accumulates and total_net_profit > 0.
    The net_profit_bps sign mirrors total_net_profit.
    """
    profit = Profit(cost=Decimal("0.98"), gross_profit=Decimal("0.02"), fees=ZERO)
    assert profit.net_profit == Decimal("0.02")  # per set, before gas

    gas = Decimal("5")  # $5 per execution (fixed)

    # Small size: 1 set -> total_net = 1 * 0.02 - 5 = -4.98  (gas wipes the edge)
    opp_small = make_opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="b2-small",
        condition_ids=["0x1"],
        legs=[],
        profit=profit,
        executable_size=ONE,
        realizes="instant",
        gas=gas,
    )
    assert opp_small.total_net_profit == Decimal("0.02") - Decimal("5")
    assert opp_small.total_net_profit < ZERO
    assert opp_small.net_profit_bps < ZERO

    # Large size: 1000 sets -> total_net = 1000 * 0.02 - 5 = 20 - 5 = 15  (edge survives)
    opp_large = make_opportunity(
        detector=DetectorKind.COMPLEMENT,
        description="b2-large",
        condition_ids=["0x1"],
        legs=[],
        profit=profit,
        executable_size=Decimal(1000),
        realizes="instant",
        gas=gas,
    )
    assert opp_large.total_net_profit == Decimal("15")
    assert opp_large.total_net_profit > ZERO
    assert opp_large.net_profit_bps > ZERO

    # Per-set net_profit is unchanged regardless of size or gas
    assert opp_small.net_profit == Decimal("0.02")
    assert opp_large.net_profit == Decimal("0.02")


# ── Committee follow-up: Fix 4 — detector suppresses gas-negative opps ───────────────────


def test_complement_detector_suppresses_gas_negative_opp() -> None:
    """ComplementDetector must not emit an opp whose total net is wiped out by gas.

    Setup: YES ask 0.499, NO ask 0.500 → net_profit = 0.001/set, size = 100.
    With gas=$5: total_net = 100 * 0.001 - 5 = 0.1 - 5 = -4.9 < 0 → suppressed.
    With gas=$0: total_net = 0.1 > 0 → emitted.
    """
    from polyarb.detectors.complement import ComplementDetector

    market = make_market("0xG", yes="GY", no="GN", fee_rate=None)
    books = {
        "GY": make_book("GY", asks=[("0.499", "100")], bids=[("0.10", "50")]),
        "GN": make_book("GN", asks=[("0.500", "100")], bids=[("0.10", "50")]),
    }

    # Gas high enough to wipe the edge entirely → nothing emitted.
    snap_with_gas = Snapshot(markets=[market], books=books, gas=Decimal("5"))
    assert list(ComplementDetector().detect(snap_with_gas)) == []

    # Drop gas to zero → the thin edge survives and an opp is emitted.
    snap_no_gas = Snapshot(markets=[market], books=books, gas=ZERO)
    opps = list(ComplementDetector().detect(snap_no_gas))
    under_opps = [o for o in opps if "under" in o.description]
    assert len(under_opps) == 1


# ── Hardening: negrisk + dependency gas-guard and crossed-book skip ───────────────────────


# Fix 4b — negrisk_basket detector must suppress gas-negative opportunities.
def test_negrisk_detector_suppresses_gas_negative_opp() -> None:
    """NegRiskBasketDetector must not emit when size * net_profit ≤ gas.

    Three outcomes at 0.30 each: net=0.10/set, size=100 → total_net=10.00.
    With gas=10.50 (wipes the edge): suppress.
    With gas=0 (no gas cost): emit.
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(3)
    ]
    event = make_event(markets, neg_risk=True)
    books = {f"y{i}": make_book(f"y{i}", asks=[("0.30", "100")]) for i in range(3)}

    snap_high_gas = Snapshot(event=event, books=books, gas=Decimal("10.50"))
    assert list(NegRiskBasketDetector().detect(snap_high_gas)) == []

    snap_no_gas = Snapshot(event=event, books=books, gas=ZERO)
    opps = list(NegRiskBasketDetector().detect(snap_no_gas))
    assert len(opps) == 1


# Fix 5 — negrisk_basket detector must skip an event with a crossed YES book.
def test_negrisk_detector_skips_crossed_yes_book() -> None:
    """NegRiskBasketDetector must yield nothing when any outcome's YES book is crossed.

    y0 has bid 0.70 >= ask 0.25 (crossed). The basket sum (0.25+0.30+0.30=0.85) would
    look profitable, but the crossed book signals bad data — must suppress.
    """
    markets = [
        make_market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group_item_title=f"O{i}")
        for i in range(3)
    ]
    event = make_event(markets, neg_risk=True)
    books = {
        "y0": make_book("y0", bids=[("0.70", "100")], asks=[("0.25", "100")]),  # crossed
        "y1": make_book("y1", asks=[("0.30", "100")]),
        "y2": make_book("y2", asks=[("0.30", "100")]),
    }
    snap = Snapshot(event=event, books=books)
    assert list(NegRiskBasketDetector().detect(snap)) == []


# Fix 4c — dependency detector must suppress gas-negative opportunities.
def test_dependency_detector_suppresses_gas_negative_opp() -> None:
    """DependencyDetector must not emit when size * net_profit ≤ gas.

    a_no_ask=0.30 (50 shares), b_yes_ask=0.30 (80 shares).
    size=50, net=0.40/set → total_net=20.00.
    With gas=25.00 (wipes the edge): suppress.
    With gas=0: emit.
    """
    market_a = make_market("0xA", yes="yA", no="nA", fee_type="crypto_fees_v2")
    market_b = make_market("0xB", yes="yB", no="nB", fee_type="crypto_fees_v2")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    books = {
        "nA": make_book("nA", asks=[("0.30", "50")]),
        "yB": make_book("yB", asks=[("0.30", "80")]),
    }
    markets = [market_a, market_b]
    relations = [relation]

    snap_high_gas = Snapshot(markets=markets, relations=relations, books=books, gas=Decimal("25"))
    assert list(DependencyDetector().detect(snap_high_gas)) == []

    snap_no_gas = Snapshot(markets=markets, relations=relations, books=books, gas=ZERO)
    opps = list(DependencyDetector().detect(snap_no_gas))
    assert len(opps) == 1


# Fix 6 — dependency detector must skip a relation with a crossed leg book.
def test_dependency_detector_skips_crossed_book() -> None:
    """DependencyDetector must yield nothing when either leg's book is crossed.

    Test both legs: crossed NO_A (case a) and crossed YES_B (case b).
    """
    market_a = make_market("0xA", yes="yA", no="nA", fee_type="crypto_fees_v2")
    market_b = make_market("0xB", yes="yB", no="nB", fee_type="crypto_fees_v2")
    relation = Relation("0xA", "0xB", "A ⇒ B")
    markets = [market_a, market_b]
    relations = [relation]

    # Case a: NO_A book is crossed (bid 0.80 >= ask 0.20).
    snap_a = Snapshot(
        markets=markets,
        relations=relations,
        books={
            "nA": make_book("nA", bids=[("0.80", "100")], asks=[("0.20", "100")]),  # crossed
            "yB": make_book("yB", asks=[("0.30", "80")]),
        },
    )
    assert list(DependencyDetector().detect(snap_a)) == []

    # Case b: YES_B book is crossed (bid 0.80 >= ask 0.20).
    snap_b = Snapshot(
        markets=markets,
        relations=relations,
        books={
            "nA": make_book("nA", asks=[("0.30", "50")]),
            "yB": make_book("yB", bids=[("0.80", "100")], asks=[("0.20", "100")]),  # crossed
        },
    )
    assert list(DependencyDetector().detect(snap_b)) == []


# ── Third adversarial bug-hunt (2026-06-30) ──────────────────────────────────

from polyarb.models import BookLevel  # noqa: E402
from polyarb.pricing.sizing import walk_sell_legs  # noqa: E402
from polyarb.resolution.relations import generate_dag_relations  # noqa: E402


# Bug 15 — list-typed Market fields must tolerate explicit JSON null / "null" (one bad market
# would otherwise crash the whole Gamma page's model_validate).
def test_market_list_fields_tolerate_null() -> None:
    base = {"id": "1", "conditionId": "0x1", "question": "q"}

    def field(payload: dict, attr: str) -> list:
        return getattr(Market.model_validate({**base, **payload}), attr)

    assert field({"clobTokenIds": None}, "clob_token_ids") == []
    assert field({"outcomes": None}, "outcomes") == []
    assert field({"umaResolutionStatuses": "null"}, "uma_resolution_statuses") == []
    assert field({"umaResolutionStatuses": None}, "uma_resolution_statuses") == []


# Bug 16 — best_ask/best_bid must skip non-positive *price* levels (consistency with the walk /
# is_crossed / top_level_min_depth), so a degenerate price=0 level can't become the "best" quote.
def test_best_ask_skips_zero_price_level() -> None:
    book = OrderBook(
        market="c",
        asset_id="t",
        timestamp_ms=1,
        bids=[
            BookLevel(price=Decimal(0), size=Decimal(100)),
            BookLevel(price=Decimal("0.30"), size=Decimal(5)),
        ],
        asks=[
            BookLevel(price=Decimal(0), size=Decimal(100)),
            BookLevel(price=Decimal("0.40"), size=Decimal(10)),
        ],
    )
    assert book.best_ask is not None and book.best_ask.price == Decimal("0.40")
    assert book.best_bid is not None and book.best_bid.price == Decimal("0.30")


# Bug 17 — walk_sell_legs returns zero on empty input (mirror walk_buy_legs; no crash).
def test_walk_sell_legs_empty_returns_zero() -> None:
    assert walk_sell_legs([], Decimal("0.02")) == (Decimal(0), [], Decimal(0))


# Bug 18 — add_relation dedupes on the (antecedent, consequent) pair (no duplicate opps).
def test_add_relation_dedupes() -> None:
    from polyarb.resolution import relations as relmod

    saved = list(relmod.SEED_RELATIONS)
    try:
        first = add_relation("0xQ1", "0xQ2", "first")
        dup = add_relation("0xQ1", "0xQ2", "duplicate")
        n = sum(
            1
            for r in relmod.SEED_RELATIONS
            if r.antecedent_condition_id == "0xQ1" and r.consequent_condition_id == "0xQ2"
        )
        assert n == 1
        # First declaration wins: the duplicate call returns the REGISTERED relation (desc
        # "first"), not a new object — a caller's captured ref matches what's in the graph.
        assert dup is first
        assert dup.description == "first"
    finally:
        relmod.SEED_RELATIONS[:] = saved


# Bug 19 — a duplicate DAG node id within one underlying must fail loud, not silently drop a market.
def test_generate_dag_rejects_duplicate_node_id() -> None:
    dupe = [
        MarketTags("a", "ETH", Comparator.NESTING, "node1", ComparatorKind.CUMULATIVE_TOUCH, "fp"),
        MarketTags("b", "ETH", Comparator.NESTING, "node1", ComparatorKind.CUMULATIVE_TOUCH, "fp"),
    ]
    with pytest.raises(ValueError):
        generate_dag_relations(dupe, [])
