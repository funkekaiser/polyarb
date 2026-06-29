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

from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar, Literal, Protocol, runtime_checkable

from polyarb.models import DetectorKind, Event, Leg, Market, Opportunity, OrderBook
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
    gas: Decimal = ZERO  # per-execution (fixed) round-trip gas estimate in USDC
    days_to_resolution: dict[str, int] = field(default_factory=dict)  # condition_id -> days


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
    days_to_resolution: int | None = None,
    gas: Decimal = ZERO,
) -> Opportunity:
    """Assemble an Opportunity, computing gas-adjusted bps and annualized return.

    ``gas`` is a fixed per-execution cost (one tx regardless of set count). All per-set
    fields (cost, gross_profit, fees, net_profit) remain clean per-set; the gas cost and the
    resulting gas-adjusted totals are computed here at the execution level.
    """
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
