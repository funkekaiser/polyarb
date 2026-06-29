"""Offline, deterministic guided demo of the polyarb pipeline.

No network. It builds three synthetic-but-realistic Polymarket scenarios — one per
detector — each containing a genuine structural arbitrage, then runs them through the
REAL pipeline (detect → tag risk → filter → rank) and prints the ranked feed plus the
full per-set profit breakdown and the filter's drop accounting.

Run:  UV_NO_SYNC=1 uv run python scripts/demo.py

This exercises the same detector/filter/ranking code the live `scan` uses; only the
data source differs (hand-built books instead of the CLOB), so the math you see here is
exactly the math the scanner applies to live books.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# Self-bootstrap: put src/ on the path so this runs even when the editable install's
# polyarb.pth is unreadable (on macOS, `uv run` re-hides the .pth and Python 3.12 skips
# hidden .pth files). Mirrors the pytest `pythonpath = ["src"]` setting.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from polyarb.config import Settings  # noqa: E402  (after the src bootstrap above)
from polyarb.detectors.base import Snapshot  # noqa: E402
from polyarb.detectors.complement import ComplementDetector  # noqa: E402
from polyarb.detectors.dependency import DependencyDetector  # noqa: E402
from polyarb.detectors.negrisk_basket import NegRiskBasketDetector  # noqa: E402
from polyarb.engine.filters import OpportunityFilter  # noqa: E402
from polyarb.engine.ranking import rank  # noqa: E402
from polyarb.models import BookLevel, Event, Market, Opportunity, OrderBook  # noqa: E402
from polyarb.resolution.risk import aggregate_risk  # noqa: E402


def book(token: str, *, asks=(), bids=(), neg_risk=False) -> OrderBook:
    return OrderBook(
        market="0xcond",
        asset_id=token,
        timestamp_ms=1,
        bids=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids],
        asks=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks],
        neg_risk=neg_risk,
    )


def market(cond: str, *, yes: str, no: str, neg_risk=False, group=None) -> Market:
    return Market(
        id=cond,
        condition_id=cond,
        question=f"{cond}?",
        outcomes=["Yes", "No"],
        clob_token_ids=[yes, no],
        neg_risk=neg_risk,
        group_item_title=group,
    )


def rule(title: str) -> None:
    print(f"\n\033[1m{'─' * 78}\n{title}\n{'─' * 78}\033[0m")


def show(opp: Opportunity) -> None:
    print(
        f"  cost/set={opp.cost}  gross={opp.gross_profit}  fees={opp.fees}  "
        f"gas={opp.gas}  net={opp.net_profit}  ({opp.net_profit_bps:.0f} bps)\n"
        f"  size={opp.executable_size} sets  realizes={opp.realizes}  "
        f"risk={opp.resolution_risk}  total net≈${opp.net_profit * opp.executable_size}"
    )


# ── Scenario 1: complement — within one binary market, YES + NO < 1 ──────────────
def complement_scenario() -> list[Opportunity]:
    rule("1. COMPLEMENT  —  YES ask + NO ask < 1  (buy both, locked $1 payoff)")
    m = market("0xCOMP", yes="cY", no="cN")
    snap = Snapshot(
        markets=[m],
        books={
            "cY": book("cY", asks=[("0.40", "100")], bids=[("0.30", "100")]),
            "cN": book("cN", asks=[("0.50", "100")], bids=[("0.40", "100")]),
        },
    )
    print("  YES ask 0.40 + NO ask 0.50 = 0.90 < 1.00  →  buy 1 of each for $0.90,")
    print("  exactly one resolves to $1. Risk-free $0.10/set, realized instantly.")
    return list(ComplementDetector().detect(snap))


# ── Scenario 2: NegRisk basket — N≥3 mutually-exclusive YES asks sum < 1 ──────────
def negrisk_scenario() -> list[Opportunity]:
    rule("2. NEGRISK BASKET  —  Σ YES asks across exclusive outcomes < 1")
    markets = [
        market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True, group=f"Outcome {i}")
        for i in range(3)
    ]
    event = Event(id="9", title="3-way race", neg_risk=True, enable_neg_risk=True, markets=markets)
    snap = Snapshot(
        event=event,
        books={f"y{i}": book(f"y{i}", asks=[("0.30", "100")]) for i in range(3)},
        days_to_resolution={"0x0": 180},
    )
    print("  Three exclusive outcomes priced 0.30 each → basket costs 0.90 for a")
    print("  guaranteed $1 at resolution. Net $0.10/set; annualized over 180 days.")
    return list(NegRiskBasketDetector().detect(snap))


# ── Scenario 3: dependency — A ⇒ B but priced as if independent ───────────────────
def dependency_scenario() -> list[Opportunity]:
    rule("3. DEPENDENCY  —  declared A ⇒ B violated  (buy YES_B + NO_A)")
    from polyarb.resolution.relations import Relation

    a = market("0xA", yes="yA", no="nA")
    b = market("0xB", yes="yB", no="nB")
    snap = Snapshot(
        markets=[a, b],
        relations=[Relation("0xA", "0xB", "wins presidency ⇒ wins nomination")],
        books={
            "nA": book("nA", asks=[("0.30", "50")]),
            "yB": book("yB", asks=[("0.30", "80")]),
        },
    )
    print("  If A⇒B, then NO_A + YES_B must cover every outcome (cost should be ≥1).")
    print("  Priced 0.30 + 0.30 = 0.60 → locked $0.40/set at resolution; size=50 (thin leg).")
    return list(DependencyDetector().detect(snap))


def main() -> None:
    print("\n\033[1mpolyarb — offline pipeline demo  (detect → tag → filter → rank)\033[0m")
    print("Read-only. No network, no signing client. Synthetic books, real detector math.")

    builders = {
        "0xCOMP": [market("0xCOMP", yes="cY", no="cN")],
        "0x0": [market(f"0x{i}", yes=f"y{i}", no=f"n{i}", neg_risk=True) for i in range(3)],
        "0xA": [market("0xA", yes="yA", no="nA")],
        "0xB": [market("0xB", yes="yB", no="nB")],
    }
    by_condition = {m.condition_id: m for ms in builders.values() for m in ms}

    opps: list[Opportunity] = []
    for scenario in (complement_scenario, negrisk_scenario, dependency_scenario):
        found = scenario()
        for opp in found:
            opp.resolution_risk = aggregate_risk(
                [by_condition[c] for c in opp.condition_ids if c in by_condition]
            )
            show(opp)
        opps.extend(found)

    rule("FILTER + RANK  —  the emitted feed (best-first)")
    settings = Settings()
    filt = OpportunityFilter(settings)
    kept = rank(filt.apply(opps))
    print(
        f"  thresholds: min_profit={settings.min_profit_bps}bps  "
        f"min_notional=${settings.min_notional_usdc}  "
        f"exclude_at_risk={settings.exclude_at_risk_resolution}"
    )
    print(f"  detected={len(opps)}  emitted={len(kept)}  drops={vars(filt.stats)}\n")
    for i, opp in enumerate(kept, 1):
        print(
            f"  #{i}  [{opp.detector}]  {opp.net_profit_bps:.0f} bps  "
            f"size={opp.executable_size}  risk={opp.resolution_risk}  "
            f"realizes={opp.realizes}\n       {opp.description}"
        )
    print()


if __name__ == "__main__":
    main()
