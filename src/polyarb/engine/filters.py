"""Emission filters: profit threshold, executable notional, resolution risk, dedupe/cooldown.

These turn raw detector output into a clean, non-noisy feed (SPEC "Shared filters"):

- **Fee/profit threshold** — emit only if ``net_profit_bps >= MIN_PROFIT_BPS``.
- **Executable notional** — reject opps whose ``executable_size * cost < MIN_NOTIONAL`` (never
  report a one-share arb).
- **Resolution-risk gate** — drop ``AT_RISK`` opps when configured.
- **Dedupe / cooldown** — don't re-alert the same opportunity every loop; key on
  (detector, condition_ids, price-bucket) within a cooldown window.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from polyarb.config import Settings
from polyarb.models import Opportunity
from polyarb.resolution.risk import ResolutionRisk

ZERO = Decimal(0)


def opportunity_key(opp: Opportunity) -> str:
    """Dedupe key: same detector + markets + price bucket → considered the same opp."""
    bucket = opp.cost.quantize(Decimal("0.01"))
    conditions = ",".join(sorted(opp.condition_ids))
    return f"{opp.detector}|{conditions}|{bucket}"


@dataclass
class DedupeCache:
    """Remembers recently-emitted opportunity keys for ``cooldown_seconds``."""

    cooldown_seconds: float
    now: Callable[[], float] = time.monotonic
    _seen: dict[str, float] = field(default_factory=dict)

    def should_emit(self, opp: Opportunity) -> bool:
        """True if this opp hasn't been emitted within the cooldown; records it when True."""
        key = opportunity_key(opp)
        now = self.now()
        last = self._seen.get(key)
        if last is not None and (now - last) < self.cooldown_seconds:
            return False
        self._seen[key] = now
        return True


@dataclass
class FilterStats:
    seen: int = 0
    below_profit: int = 0
    below_notional: int = 0
    at_risk: int = 0
    deduped: int = 0
    emitted: int = 0


class OpportunityFilter:
    """Applies the emission filters in order; tracks why opps were dropped."""

    def __init__(self, settings: Settings, dedupe: DedupeCache | None = None) -> None:
        self._settings = settings
        self._dedupe = dedupe or DedupeCache(settings.dedupe_cooldown_seconds)
        self.stats = FilterStats()

    def passes(self, opp: Opportunity) -> bool:
        s = self._settings
        self.stats.seen += 1

        if opp.net_profit_bps < s.min_profit_bps:
            self.stats.below_profit += 1
            return False

        notional = opp.executable_size * opp.cost
        if notional < s.min_notional_usdc:
            self.stats.below_notional += 1
            return False

        if s.exclude_at_risk_resolution and opp.resolution_risk == ResolutionRisk.AT_RISK:
            self.stats.at_risk += 1
            return False

        if not self._dedupe.should_emit(opp):
            self.stats.deduped += 1
            return False

        self.stats.emitted += 1
        return True

    def apply(self, opps: list[Opportunity]) -> list[Opportunity]:
        return [opp for opp in opps if self.passes(opp)]
