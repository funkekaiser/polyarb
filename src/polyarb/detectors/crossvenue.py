"""Cross-venue arbitrage — STUB ONLY. Deliberately not implemented.

Polymarket vs another venue carries two risks that must be resolved before any opp is
emitted: (1) **resolution-source equivalence** — the two venues may resolve "the same"
question by different sources/criteria, so a "locked" position isn't actually locked; and
(2) a **jurisdiction** problem. ``resolution_equivalence_check`` must pass before emitting.

TODO: implement venue #2 only after a rigorous resolution-equivalence model exists and the
jurisdiction question is settled. Until then this raises rather than silently returning.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from polyarb.detectors.base import Snapshot
from polyarb.models import DetectorKind, Opportunity


def resolution_equivalence_check(venue_a_market: object, venue_b_market: object) -> bool:
    """Must return True (provably-equivalent resolution sources) before any cross-venue opp.

    Not implemented: there is no verified model of cross-venue resolution equivalence yet.
    """
    raise NotImplementedError(
        "cross-venue resolution-equivalence check is not implemented; see crossvenue.py TODO"
    )


class CrossVenueDetector:
    # PLACEHOLDER kind — there is no cross-venue DetectorKind yet. This detector is a stub and
    # must NOT be added to the scanner's detector list; if it ever is, give it its own kind so
    # logs/metrics don't mis-attribute it to `dependency`.
    kind: ClassVar[DetectorKind] = DetectorKind.DEPENDENCY

    def detect(self, snap: Snapshot) -> Iterator[Opportunity]:
        raise NotImplementedError(
            "cross-venue detection is a deliberate stub (resolution-equivalence + jurisdiction "
            "risk). Do not enable without resolution_equivalence_check passing."
        )
