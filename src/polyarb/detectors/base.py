"""Detector protocol, the shared input snapshot, and profit/opportunity helpers.

Each detector consumes a :class:`Snapshot` (markets + their order books, plus declared
relations and cost params) and yields :class:`~polyarb.models.Opportunity` objects. The
profit *math* is kept in pure functions (in each detector module) that return a
:class:`Profit`; property tests target those directly. Detectors emit an opportunity only
when ``net_profit > 0`` — a structurally-violated identity that is still profitable after
fees (per set, before gas). Gas is a fixed per-execution cost applied in
:func:`make_opportunity`. Threshold/size/resolution filtering layers on top in the engine
(Phase 3).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar, Literal, Protocol, runtime_checkable

from polyarb.models import BookLevel, DetectorKind, Event, Leg, Market, Opportunity, OrderBook
from polyarb.pricing.sizing import walk_buy_legs
from polyarb.resolution.relations import Relation

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)


@dataclass(frozen=True)
class Profit:
    """Per-set profit breakdown, before gas. ``net_profit = gross_profit - fees``."""

    cost: Decimal
    gross_profit: Decimal
    fees: Decimal

    @property
    def net_profit(self) -> Decimal:
        return self.gross_profit - self.fees


@dataclass
class Snapshot:
    """Everything a detector needs for one scan pass, already fetched."""

    books: dict[str, OrderBook] = field(default_factory=dict)  # token_id -> book
    event: Event | None = None
    markets: list[Market] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    gas: Decimal = ZERO  # fixed per-execution component (merge/redeem), USDC
    gas_per_leg: Decimal = ZERO  # per-leg component (each leg ≈ one taker fill) (B2')
    days_to_resolution: dict[str, int] = field(default_factory=dict)  # condition_id -> days

    def gas_for(self, n_legs: int) -> Decimal:
        """Per-execution gas for an ``n_legs`` arb: fixed base + per-leg (B2').

        A 2-leg complement and a 12-leg basket don't cost the same on-chain (N taker fills +
        a merge/redeem), so gas scales with leg count. Both components default to 0, so until
        they're configured this is exactly the old flat behavior (`gas`)."""
        return self.gas + self.gas_per_leg * n_legs


@runtime_checkable
class Detector(Protocol):
    kind: ClassVar[DetectorKind]

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]: ...


def make_opportunity(
    *,
    detector: DetectorKind,
    description: str,
    condition_ids: list[str],
    legs: list[Leg],
    profit: Profit,
    executable_size: Decimal,
    realizes: Literal["instant", "resolution"],
    event_id: str | None = None,
    days_by_condition: Mapping[str, int] | None = None,
    gas: Decimal = ZERO,
) -> Opportunity:
    """Assemble an Opportunity, computing gas-adjusted bps and annualized return.

    ``gas`` is the per-execution cost (leg-count-scaled upstream via ``Snapshot.gas_for``). All
    per-set fields (cost, gross_profit, fees, net_profit) remain clean per-set; gas and the
    resulting gas-adjusted totals are computed here at the execution level.

    D3 — a held arb locks capital until its *latest* leg resolves, so the horizon is the **max**
    ``days_to_resolution`` over the legs the opp actually spans (``condition_ids``), not the
    first/either leg. ``days_by_condition`` maps condition_id → days; we take the max over the
    present spanned legs (None if none are known). Centralized here so every detector is
    consistent and scoped to the opp's own legs.
    """
    spanned_days = (
        [days_by_condition[c] for c in condition_ids if c in days_by_condition]
        if days_by_condition
        else []
    )
    days_to_resolution = max(spanned_days) if spanned_days else None

    net_set = profit.net_profit  # per set, before gas
    total_cost = executable_size * profit.cost
    total_net = executable_size * net_set - gas
    net_profit_bps = (total_net / total_cost * BPS) if total_cost > ZERO else ZERO

    annualized: Decimal | None = None
    if realizes == "resolution" and days_to_resolution is not None and total_cost > ZERO:
        # days_to_resolution == 0 means "resolves today" — floor at 1 day so it annualizes to
        # a high (not None) value and ranks near the top, and to avoid a divide-by-zero.
        days = Decimal(max(days_to_resolution, 1))
        annualized = (total_net / total_cost) * (Decimal(365) / days)
    return Opportunity(
        detector=detector,
        description=description,
        event_id=event_id,
        condition_ids=condition_ids,
        legs=legs,
        cost=profit.cost,
        gross_profit=profit.gross_profit,
        fees=profit.fees,
        gas=gas,
        net_profit=net_set,
        net_profit_bps=net_profit_bps,
        executable_size=executable_size,
        realizes=realizes,
        days_to_resolution=days_to_resolution,
        annualized=annualized,
    )


def walk_and_size_buy_basket(
    ask_levels: list[list[BookLevel]],
    fee_rates: Decimal | Sequence[Decimal],
    gas: Decimal,
    payoff: Decimal = ONE,
) -> tuple[Decimal, list[Decimal], Profit] | None:
    """Depth-walk a set of buy legs, size them at VWAP, and apply the gas-realizability guard.

    Shared by the buy-side detectors (complement-under, negrisk basket, dependency): each buys
    one share of every leg per set for a set worth ``payoff``. Returns ``(size, leg_costs,
    profit)`` — per-set economics over ``size`` sets — or ``None`` when there is no profitable
    depth or the total net (``size * net_profit``) doesn't clear the fixed per-execution ``gas``.
    """
    size, leg_costs, fees = walk_buy_legs(ask_levels, fee_rates, payoff=payoff)
    if size <= ZERO:
        return None
    cost_ps = sum(leg_costs, ZERO) / size
    profit = Profit(cost=cost_ps, gross_profit=payoff - cost_ps, fees=fees / size)
    if size * profit.net_profit - gas <= ZERO:
        return None
    return size, leg_costs, profit
