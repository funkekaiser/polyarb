"""Declared logical-dependency relations between markets.

A relation ``A ⇒ B`` asserts that whenever A resolves YES, B must too — so the identity
``P(A) ≤ P(B)`` must hold. The dependency detector flags violations (``price(A) > price(B)``)
and prices the locked trade *buy YES_B + buy NO_A*.

Dependencies are **declared, never inferred from text** (SPEC constraint). Relations are
keyed by on-chain ``condition_id``; adding one is a one-liner via :func:`add_relation`. The
seed list ships empty because condition_ids are market-instance-specific.

The full design for this subsystem is ``docs/RELATIONS.md`` — it specifies two edge
mechanisms, a prioritized seed set, exclusions, and the resolution-fingerprint gate.

Auto-generated relations use two paths:

* **Ladder generator** (:func:`generate_ladder_relations`): markets tagged with
  ``Comparator.BY_DATE``, ``THRESHOLD_GTE``, or ``THRESHOLD_LTE`` and
  ``ComparatorKind.CUMULATIVE_TOUCH`` sort into chains; adjacent-rung ``Relation`` objects are
  emitted (transitivity means only adjacent rungs need flagging). Markets with
  ``POINT_IN_TIME`` kind are excluded (RELATIONS.md §3/§4 trap).

* **DAG generator** (:func:`generate_dag_relations`): sports-round and political-stage
  nesting is a partial order, not a total chain. Declare the minimal edge set
  (``SPORTS_NESTING``, ``POLITICS_NESTING``) and call :func:`transitive_closure`; the
  generator emits one ``Relation`` per reachable pair whose markets both exist and share a
  ``resolution_fingerprint`` (§6 gate).

In both generators, no ``Relation`` is ever emitted across markets with differing
``resolution_fingerprint`` (§6 gate applied at generation time).
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# Core relation — consumed by detectors/dependency.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Relation:
    """``antecedent ⇒ consequent`` (A ⇒ B), so ``P(A) ≤ P(B)`` must hold.

    ``antecedent_condition_id`` / ``consequent_condition_id`` are on-chain market ids.
    Examples of valid relations: "by date X" ⇒ "by date Y" for X earlier than Y;
    wins-presidency ⇒ wins-nomination; wins-championship ⇒ makes-playoffs.
    """

    antecedent_condition_id: str  # A — the specific/stronger leg (should be cheaper)
    consequent_condition_id: str  # B — the general/weaker leg (should be richer)
    description: str


# Hand-curated seed graph. Populate with real condition_ids for the markets you track, e.g.:
#   add_relation("0x<presidency>", "0x<nomination>", "wins presidency ⇒ wins nomination")
SEED_RELATIONS: list[Relation] = []


def add_relation(antecedent: str, consequent: str, description: str) -> Relation:
    """Declare and register a relation in the seed graph; returns it."""
    relation = Relation(antecedent, consequent, description)
    SEED_RELATIONS.append(relation)
    return relation


# ---------------------------------------------------------------------------
# Tag schema (RELATIONS.md §3)
# ---------------------------------------------------------------------------


class Comparator(StrEnum):
    """How a market's bound relates to the underlying value."""

    BY_DATE = "by_date"  # cumulative-touch deadline ladder
    THRESHOLD_GTE = "threshold_gte"  # "≥ X" on a fixed date
    THRESHOLD_LTE = "threshold_lte"  # "≤ X" on a fixed date
    NESTING = "nesting"  # declared DAG node
    WINDOW = "window"  # narrow window implies container


class ComparatorKind(StrEnum):
    """Cumulative vs point-in-time — only ``cumulative_touch`` markets ladder (§3/§4)."""

    CUMULATIVE_TOUCH = "cumulative_touch"
    POINT_IN_TIME = "point_in_time"


@dataclass(frozen=True)
class MarketTags:
    """Five-tag schema for a single market (RELATIONS.md §3).

    ``bound`` encoding by comparator:

    * ``BY_DATE`` — ISO date string ``"YYYY-MM-DD"``.
    * ``THRESHOLD_GTE`` / ``THRESHOLD_LTE`` — numeric string e.g. ``"150000"``.
    * ``NESTING`` — DAG node id e.g. ``"make_playoffs"``.
    * ``WINDOW`` — human label (not consumed by the current generators).
    """

    condition_id: str
    underlying_key: str  # canonical subject; only same-key markets compare
    comparator: Comparator
    bound: str  # deadline / numeric threshold / DAG node id
    comparator_kind: ComparatorKind
    resolution_fingerprint: str  # settlement source + cutoff + timezone + index


# Hand-declared market tags for ladder/DAG generation. Populate per tracked market — a
# curation task like SEED_RELATIONS (tags are declared, never inferred from text). The
# scanner picks these up by default to build its dependency edges.
TAG_REGISTRY: list[MarketTags] = []


# ---------------------------------------------------------------------------
# Ladder generator (RELATIONS.md §2a, §3, §4)
# ---------------------------------------------------------------------------

_LADDER_COMPARATORS: frozenset[Comparator] = frozenset(
    {Comparator.BY_DATE, Comparator.THRESHOLD_GTE, Comparator.THRESHOLD_LTE}
)


def generate_ladder_relations(tags: list[MarketTags]) -> list[Relation]:
    """Auto-generate adjacent-rung ``Relation`` objects from total-order ladders.

    Only ``BY_DATE``, ``THRESHOLD_GTE``, and ``THRESHOLD_LTE`` comparators form ladders.
    Only ``CUMULATIVE_TOUCH`` markets participate — ``POINT_IN_TIME`` is silently excluded
    (RELATIONS.md §4 trap). Within each ``(underlying_key, comparator)`` group, markets are
    split by ``resolution_fingerprint`` so no cross-fingerprint relation is ever emitted (§6).

    Sort and direction logic — sign convention §1: antecedent is the specific/stronger leg
    (price(A) ≤ price(B)):

    * ``BY_DATE``: sort ascending by date. An *earlier* deadline is harder to satisfy, so it
      is the antecedent. For adjacent pair ``(earlier, later)``:
      ``Relation(antecedent=earlier, consequent=later)``.

    * ``THRESHOLD_GTE``: sort ascending by numeric threshold. A *higher* ``≥``-threshold is
      harder to satisfy, so it is the antecedent. For adjacent pair ``(lower, higher)``:
      ``Relation(antecedent=higher, consequent=lower)``.

    * ``THRESHOLD_LTE``: sort ascending by numeric threshold. A *lower* ``≤``-threshold is
      harder to satisfy, so it is the antecedent. For adjacent pair ``(lower, higher)``:
      ``Relation(antecedent=lower, consequent=higher)``.
    """
    # Filter to ladder-capable, cumulative-touch tags only
    candidates = [
        t
        for t in tags
        if t.comparator in _LADDER_COMPARATORS
        and t.comparator_kind == ComparatorKind.CUMULATIVE_TOUCH
    ]

    # Group by (underlying_key, comparator, resolution_fingerprint) — each fingerprint cohort
    # is laddered independently so the §6 gate is enforced at generation time.
    cohorts: dict[tuple[str, Comparator, str], list[MarketTags]] = defaultdict(list)
    for t in candidates:
        key = (t.underlying_key, t.comparator, t.resolution_fingerprint)
        cohorts[key].append(t)

    relations: list[Relation] = []
    for (underlying_key, comparator, _fp), cohort in cohorts.items():
        if len(cohort) < 2:
            continue

        try:
            if comparator == Comparator.BY_DATE:
                # Earlier deadline ⇒ later deadline (earlier is the stronger/antecedent leg)
                ordered = sorted(cohort, key=lambda t: datetime.date.fromisoformat(t.bound))
                for i in range(len(ordered) - 1):
                    ante, cons = ordered[i], ordered[i + 1]
                    relations.append(
                        Relation(
                            ante.condition_id,
                            cons.condition_id,
                            f"{underlying_key} ladder: {ante.bound} ⇒ {cons.bound}",
                        )
                    )

            elif comparator == Comparator.THRESHOLD_GTE:
                # Higher ≥-threshold ⇒ lower ≥-threshold (higher is the stronger/antecedent)
                ordered = sorted(cohort, key=lambda t: float(t.bound))
                for i in range(len(ordered) - 1):
                    lower, higher = ordered[i], ordered[i + 1]
                    relations.append(
                        Relation(
                            higher.condition_id,
                            lower.condition_id,
                            f"{underlying_key} ladder: {higher.bound} ⇒ {lower.bound}",
                        )
                    )

            elif comparator == Comparator.THRESHOLD_LTE:
                # Lower ≤-threshold ⇒ higher ≤-threshold (lower is the stronger/antecedent)
                ordered = sorted(cohort, key=lambda t: float(t.bound))
                for i in range(len(ordered) - 1):
                    lower, higher = ordered[i], ordered[i + 1]
                    relations.append(
                        Relation(
                            lower.condition_id,
                            higher.condition_id,
                            f"{underlying_key} ladder: {lower.bound} ⇒ {higher.bound}",
                        )
                    )

        except ValueError:
            # Malformed bound — skip the cohort rather than crash the whole scan
            continue

    return relations


# ---------------------------------------------------------------------------
# DAG generator (RELATIONS.md §2b, §4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DagEdge:
    """A single declared nesting edge: ``specific`` implies ``general``.

    Matches RELATIONS.md sign convention: specific is the stronger/more-specific event
    (should be cheaper); general is the weaker/broader event (should be richer).
    """

    specific: str  # more-specific node id — stronger, should be the cheaper leg
    general: str  # more-general node id — weaker, should be the richer leg


# Sports-round partial order — §4 Priority 3. Minimal edge set only; transitive closure
# supplies (win_championship ⇒ make_playoffs) automatically.
#
# INVALID edges deliberately absent per §5:
#   win_championship → win_division  ✗  wildcard winners never need to win the division
#   win_division ↔ reach_final       ✗  incomparable: a division winner can exit round-1;
#                                       a wildcard can reach the final
SPORTS_NESTING: list[DagEdge] = [
    DagEdge("win_championship", "reach_final"),
    DagEdge("reach_final", "make_playoffs"),
    DagEdge("win_division", "make_playoffs"),
]

# Political stage chain — §4 Priority 4.
# Restrict win_presidency → win_party_nomination to declared major-party candidates only
# (an independent/third-party path breaks the implication — see RELATIONS.md §4 caveat).
POLITICS_NESTING: list[DagEdge] = [
    DagEdge("win_presidency", "win_party_nomination"),
    DagEdge("win_party_nomination", "is_candidate"),
]


def transitive_closure(edges: list[DagEdge]) -> set[tuple[str, str]]:
    """Return all ``(specific, general)`` pairs reachable via ``edges``.

    Includes both direct edges and multi-hop transitive pairs. Self-pairs are excluded.
    Uses DFS from each node; safe against cycles in the input (cyclic inputs are not expected
    for sports/politics, but the algorithm terminates correctly regardless).
    """
    # Build adjacency: node → set of immediate successors
    successors: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        successors[e.specific].add(e.general)

    all_nodes = {e.specific for e in edges} | {e.general for e in edges}
    closure: set[tuple[str, str]] = set()

    for start in all_nodes:
        # DFS: collect every node reachable from start (excluding start itself)
        reachable: set[str] = set()
        stack = list(successors[start])
        while stack:
            node = stack.pop()
            if node in reachable:
                continue
            reachable.add(node)
            stack.extend(successors[node] - reachable)
        for reached in reachable:
            if reached != start:  # exclude self-pairs (defensive against cyclic inputs)
                closure.add((start, reached))

    return closure


def generate_dag_relations(tags: list[MarketTags], edges: list[DagEdge]) -> list[Relation]:
    """Emit one ``Relation`` per closure pair whose markets both exist and share a fingerprint.

    Considers only tags with ``comparator == NESTING``; ``bound`` is the DAG node id.
    Markets are grouped by ``underlying_key`` so nodes for different teams/candidates never
    cross. The §6 fingerprint gate is enforced: a pair is skipped if the two markets carry
    differing ``resolution_fingerprint`` values.
    """
    nesting_tags = [t for t in tags if t.comparator == Comparator.NESTING]

    # Group by underlying_key; node id (bound) must be unique within an underlying
    by_underlying: dict[str, list[MarketTags]] = defaultdict(list)
    for t in nesting_tags:
        by_underlying[t.underlying_key].append(t)

    closure = transitive_closure(edges)
    relations: list[Relation] = []

    for underlying, market_tags in by_underlying.items():
        node_map: dict[str, MarketTags] = {t.bound: t for t in market_tags}

        for specific_node, general_node in closure:
            if specific_node not in node_map or general_node not in node_map:
                continue
            specific_tag = node_map[specific_node]
            general_tag = node_map[general_node]
            # §6 gate — never emit across markets with differing resolution fingerprints
            if specific_tag.resolution_fingerprint != general_tag.resolution_fingerprint:
                continue
            relations.append(
                Relation(
                    specific_tag.condition_id,
                    general_tag.condition_id,
                    f"{underlying} nesting: {specific_node} ⇒ {general_node}",
                )
            )

    return relations
