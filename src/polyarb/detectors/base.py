"""Detector protocol, the shared input snapshot, and profit/opportunity helpers.

Each detector consumes a :class:`Snapshot` (markets + their order books, plus declared
relations and cost params) and yields :class:`~polyarb.models.Opportunity` objects. The
profit *math* is kept in pure functions (in each detector module) that return a
:class:`Profit`; property tests target those directly. Detectors emit an opportunity only
when ``net_profit > 0`` — a structurally-violated identity that is still profitable after
fees and gas. Threshold/size/resolution filtering layers on top in the engine (Phase 3).
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
    """Per-set profit breakdown. ``net_profit = gross_profit - fees - gas``."""

    cost: Decimal
    gross_profit: Decimal
    fees: Decimal
    gas: Decimal

    @property
    def net_profit(self) -> Decimal:
        return self.gross_profit - self.fees - self.gas

    @property
    def net_profit_bps(self) -> Decimal:
        if self.cost <= ZERO:
            return ZERO
        return self.net_profit / self.cost * BPS


@dataclass
class Snapshot:
    """Everything a detector needs for one scan pass, already fetched."""

    books: dict[str, OrderBook] = field(default_factory=dict)  # token_id -> book
    event: Event | None = None
    markets: list[Market] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    gas: Decimal = ZERO  # per-set round-trip gas estimate
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
) -> Opportunity:
    """Assemble an Opportunity, computing bps and (for resolution arbs) annualized return."""
    annualized: Decimal | None = None
    if realizes == "resolution" and days_to_resolution and profit.cost > ZERO:
        annualized = (profit.net_profit / profit.cost) * (
            Decimal(365) / Decimal(days_to_resolution)
        )
    return Opportunity(
        detector=detector,
        description=description,
        event_id=event_id,
        condition_ids=condition_ids,
        legs=legs,
        cost=profit.cost,
        gross_profit=profit.gross_profit,
        fees=profit.fees,
        gas=profit.gas,
        net_profit=profit.net_profit,
        net_profit_bps=profit.net_profit_bps,
        executable_size=executable_size,
        realizes=realizes,
        days_to_resolution=days_to_resolution,
        annualized=annualized,
    )
