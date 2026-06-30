"""Ranking tests — safer first, then higher absolute net $ (C3), instant ahead on annualized."""

from __future__ import annotations

from decimal import Decimal

from polyarb.engine.ranking import rank
from polyarb.models import DetectorKind, Opportunity
from polyarb.resolution.risk import ResolutionRisk


def _opp(
    *,
    net: str,
    size: str,
    risk: ResolutionRisk,
    bps: str = "100",
    realizes: str = "resolution",
    annualized: str | None = None,
    live_count: int | None = None,
    total_count: int | None = None,
) -> Opportunity:
    # total_net_profit = size * net - gas; that absolute $ (not bps) is the ranking money axis.
    return Opportunity(
        detector=DetectorKind.COMPLEMENT,
        description=f"{risk}-{size}x{net}",
        condition_ids=["0x1"],
        legs=[],
        cost=Decimal("0.90"),
        gross_profit=Decimal(net),
        fees=Decimal(0),
        gas=Decimal(0),
        net_profit=Decimal(net),
        net_profit_bps=Decimal(bps),
        executable_size=Decimal(size),
        realizes=realizes,  # type: ignore[arg-type]
        annualized=Decimal(annualized) if annualized else None,
        resolution_risk=risk,
        live_count=live_count,
        total_count=total_count,
    )


def test_safer_resolution_ranks_first_even_with_less_money() -> None:
    # Risk is the primary axis: a small-$ OBJECTIVE arb still outranks a larger-$ ELEVATED one.
    risky_big = _opp(net="0.10", size="1000", risk=ResolutionRisk.ELEVATED)  # $100
    safe_small = _opp(net="0.10", size="10", risk=ResolutionRisk.OBJECTIVE)  # $1
    assert rank([risky_big, safe_small])[0] is safe_small


def test_higher_absolute_dollars_wins_within_same_risk() -> None:
    small = _opp(net="0.10", size="50", risk=ResolutionRisk.OBJECTIVE)  # $5
    big = _opp(net="0.10", size="500", risk=ResolutionRisk.OBJECTIVE)  # $50
    assert rank([small, big])[0] is big


def test_absolute_dollars_beats_bps_artifact() -> None:
    # The C3 point: a tiny-$ leg with an exploded bps (cent-cost artifact / thin book) must NOT
    # outrank a real large-$ arb with modest bps. Same risk tier so only the money axis decides.
    artifact = _opp(net="0.10", size="1", risk=ResolutionRisk.OBJECTIVE, bps="9999")  # $0.10
    real = _opp(net="0.05", size="1000", risk=ResolutionRisk.OBJECTIVE, bps="50")  # $50
    assert rank([artifact, real])[0] is real


def test_instant_ranks_ahead_of_resolution_on_annualized() -> None:
    # Equal $ and risk → instant (capital freed immediately) wins the annualized tiebreak.
    instant = _opp(net="0.10", size="100", risk=ResolutionRisk.OBJECTIVE, realizes="instant")
    resolution = _opp(net="0.10", size="100", risk=ResolutionRisk.OBJECTIVE, annualized="5")
    assert rank([resolution, instant])[0] is instant


# ---------------------------------------------------------------------------
# A1-RISKWT — live-fraction tiebreak (soft, lowest priority)
# ---------------------------------------------------------------------------


def test_near_fully_resolved_basket_ranks_below_fuller_one() -> None:
    """A1-riskwt soft tiebreak: an otherwise-equal near-fully-resolved basket ranks below a
    fully-live one. Same risk tier, same absolute $; the live fraction is the only difference."""
    fuller = _opp(
        net="0.10",
        size="100",
        risk=ResolutionRisk.OBJECTIVE,
        live_count=3,
        total_count=3,  # all 3 legs still live
    )
    near_resolved = _opp(
        net="0.10",
        size="100",
        risk=ResolutionRisk.OBJECTIVE,
        live_count=1,
        total_count=3,  # only 1 of 3 legs still live
    )
    ranked = rank([near_resolved, fuller])
    assert ranked[0] is fuller
    assert ranked[1] is near_resolved


def test_invalid_live_count_clamped_to_neutral() -> None:
    """Bug-hunt regression: an invalid live_count > total_count must NOT score better than a
    genuinely fully-live basket. The live-fraction key is clamped to the neutral floor, so the
    two tie and stable order keeps the fully-live opp first. Without the clamp the invalid opp's
    key (-5/3 < -1) would wrongly sort it ahead."""
    fully_live = _opp(
        net="0.10", size="100", risk=ResolutionRisk.OBJECTIVE, live_count=3, total_count=3
    )
    invalid = _opp(
        net="0.10", size="100", risk=ResolutionRisk.OBJECTIVE, live_count=5, total_count=3
    )
    ranked = rank([fully_live, invalid])
    assert ranked[0] is fully_live


def test_rank_uses_conservative_decision_size() -> None:
    """C1-atomicity-use: ranking sorts on the conservative decision size, so a deep-but-thin-top
    opp (huge executable, tiny best-level) ranks BELOW a genuinely fully-fillable smaller one."""
    # Phantom-deep: executable 1000 but only 5 fillable at the top → decision_net ~ 5*0.10.
    phantom = _opp(net="0.10", size="1000", risk=ResolutionRisk.OBJECTIVE)
    phantom.conservative_size = Decimal(5)
    # Genuinely deep-flat: executable 50, all 50 fillable at the top → decision_net ~ 50*0.10.
    real = _opp(net="0.10", size="50", risk=ResolutionRisk.OBJECTIVE)
    real.conservative_size = Decimal(50)
    ranked = rank([phantom, real])
    assert ranked[0] is real  # the truly-fillable opp wins, despite smaller executable_size
    assert ranked[1] is phantom


def test_riskwt_never_reorders_bigger_dollar_opp() -> None:
    """A1-riskwt: a near-fully-resolved basket must NEVER rank above a bigger-$ opp
    in the same risk tier. The absolute-$ axis dominates the live-fraction tiebreak."""
    near_resolved = _opp(
        net="0.10",
        size="100",
        risk=ResolutionRisk.OBJECTIVE,
        live_count=1,
        total_count=3,  # $10, near-resolved
    )
    bigger = _opp(
        net="0.10",
        size="1000",
        risk=ResolutionRisk.OBJECTIVE,
        # no live_count/total_count → treated as neutral (fully live)
    )  # $100, no count info
    assert rank([near_resolved, bigger])[0] is bigger


def test_riskwt_never_reorders_safer_tier_opp() -> None:
    """A1-riskwt: a near-fully-resolved basket must NEVER rank above a safer-tier opp,
    even when the safer opp has far less absolute $."""
    near_resolved = _opp(
        net="0.10",
        size="10000",
        risk=ResolutionRisk.ELEVATED,
        live_count=1,
        total_count=10,  # big $, but risky tier and near-resolved
    )
    safer_small = _opp(
        net="0.10",
        size="10",
        risk=ResolutionRisk.OBJECTIVE,  # tiny $, but safest tier
    )
    assert rank([near_resolved, safer_small])[0] is safer_small


def test_riskwt_none_live_count_is_neutral() -> None:
    """A1-riskwt: an opp with None live_count/total_count ranks the same as a fully-live
    basket — it is not penalized by the tiebreak."""
    no_count = _opp(net="0.10", size="100", risk=ResolutionRisk.OBJECTIVE)
    fully_live = _opp(
        net="0.10",
        size="100",
        risk=ResolutionRisk.OBJECTIVE,
        live_count=3,
        total_count=3,
    )
    # Both should appear in stable order (either before the other is fine); but a near-resolved
    # basket must rank below both.
    near_resolved = _opp(
        net="0.10",
        size="100",
        risk=ResolutionRisk.OBJECTIVE,
        live_count=1,
        total_count=3,
    )
    ranked = rank([near_resolved, no_count, fully_live])
    assert ranked[-1] is near_resolved  # near-resolved is last (worst on tiebreak)
