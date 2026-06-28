"""Ladder and DAG relation generators — unit tests (offline, no live API calls).

All tests exercise the tag-based auto-generation paths in
``polyarb.resolution.relations``. The sign convention throughout: antecedent is the
specific/stronger leg (should be the cheaper one), consequent is the general/weaker leg.
"""

from __future__ import annotations

from polyarb.resolution.relations import (
    POLITICS_NESTING,
    SPORTS_NESTING,
    Comparator,
    ComparatorKind,
    MarketTags,
    Relation,
    generate_dag_relations,
    generate_ladder_relations,
    transitive_closure,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tag(
    cid: str,
    underlying: str,
    comparator: Comparator,
    bound: str,
    kind: ComparatorKind = ComparatorKind.CUMULATIVE_TOUCH,
    fingerprint: str = "fp-A",
) -> MarketTags:
    return MarketTags(
        condition_id=cid,
        underlying_key=underlying,
        comparator=comparator,
        bound=bound,
        comparator_kind=kind,
        resolution_fingerprint=fingerprint,
    )


def _nesting_tag(cid: str, underlying: str, node: str, fingerprint: str = "fp-A") -> MarketTags:
    """Convenience wrapper for NESTING comparator tags."""
    return MarketTags(
        condition_id=cid,
        underlying_key=underlying,
        comparator=Comparator.NESTING,
        bound=node,
        comparator_kind=ComparatorKind.CUMULATIVE_TOUCH,  # irrelevant for NESTING path
        resolution_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# BY_DATE ladder
# ---------------------------------------------------------------------------


def test_by_date_ladder_emits_two_adjacent_relations() -> None:
    """3 cumulative_touch markets → 2 adjacent relations, earlier ⇒ later direction."""
    tags = [
        _tag("cid-jun", "BTC-touch-150k", Comparator.BY_DATE, "2026-06-30"),
        _tag("cid-sep", "BTC-touch-150k", Comparator.BY_DATE, "2026-09-30"),
        _tag("cid-dec", "BTC-touch-150k", Comparator.BY_DATE, "2026-12-31"),
    ]
    rels = generate_ladder_relations(tags)
    assert len(rels) == 2
    # Earlier deadline is antecedent (stronger/cheaper leg)
    assert rels[0] == Relation(
        "cid-jun", "cid-sep", "BTC-touch-150k ladder: 2026-06-30 ⇒ 2026-09-30"
    )
    assert rels[1] == Relation(
        "cid-sep", "cid-dec", "BTC-touch-150k ladder: 2026-09-30 ⇒ 2026-12-31"
    )


def test_by_date_excludes_point_in_time_market() -> None:
    """A 4th market tagged POINT_IN_TIME must not appear in any emitted relation."""
    tags = [
        _tag("cid-jun", "BTC-touch-150k", Comparator.BY_DATE, "2026-06-30"),
        _tag("cid-sep", "BTC-touch-150k", Comparator.BY_DATE, "2026-09-30"),
        _tag("cid-dec", "BTC-touch-150k", Comparator.BY_DATE, "2026-12-31"),
        _tag(
            "cid-pit",
            "BTC-touch-150k",
            Comparator.BY_DATE,
            "2026-12-31",
            kind=ComparatorKind.POINT_IN_TIME,
        ),
    ]
    rels = generate_ladder_relations(tags)
    # Still 2 relations — the point-in-time market is silently dropped
    assert len(rels) == 2
    condition_ids = {
        cid for r in rels for cid in (r.antecedent_condition_id, r.consequent_condition_id)
    }
    assert "cid-pit" not in condition_ids


# ---------------------------------------------------------------------------
# THRESHOLD_GTE ladder
# ---------------------------------------------------------------------------


def test_threshold_gte_emits_adjacent_higher_to_lower() -> None:
    """Higher ≥-threshold is the antecedent (stronger/cheaper). 3 markets → 2 relations."""
    tags = [
        _tag("eth-6k", "ETH-USD-dec31", Comparator.THRESHOLD_GTE, "6000"),
        _tag("eth-8k", "ETH-USD-dec31", Comparator.THRESHOLD_GTE, "8000"),
        _tag("eth-10k", "ETH-USD-dec31", Comparator.THRESHOLD_GTE, "10000"),
    ]
    rels = generate_ladder_relations(tags)
    assert len(rels) == 2
    # (6k, 8k) adjacent pair → antecedent=8k (higher/stronger), consequent=6k
    assert rels[0] == Relation("eth-8k", "eth-6k", "ETH-USD-dec31 ladder: 8000 ⇒ 6000")
    # (8k, 10k) adjacent pair → antecedent=10k (higher/stronger), consequent=8k
    assert rels[1] == Relation("eth-10k", "eth-8k", "ETH-USD-dec31 ladder: 10000 ⇒ 8000")


def test_threshold_gte_non_adjacent_pair_not_directly_emitted() -> None:
    """The non-adjacent (6k, 10k) pair must not appear — only adjacent rungs are emitted."""
    tags = [
        _tag("eth-6k", "ETH-USD-dec31", Comparator.THRESHOLD_GTE, "6000"),
        _tag("eth-8k", "ETH-USD-dec31", Comparator.THRESHOLD_GTE, "8000"),
        _tag("eth-10k", "ETH-USD-dec31", Comparator.THRESHOLD_GTE, "10000"),
    ]
    rels = generate_ladder_relations(tags)
    pairs = {(r.antecedent_condition_id, r.consequent_condition_id) for r in rels}
    assert ("eth-10k", "eth-6k") not in pairs  # non-adjacent; transitivity covers it


# ---------------------------------------------------------------------------
# THRESHOLD_LTE ladder
# ---------------------------------------------------------------------------


def test_threshold_lte_emits_adjacent_lower_to_higher() -> None:
    """Lower ≤-threshold is the antecedent (stronger/cheaper). Direction: lower ⇒ higher."""
    tags = [
        _tag("btc-4k", "BTC-USD-dec31-lte", Comparator.THRESHOLD_LTE, "4000"),
        _tag("btc-6k", "BTC-USD-dec31-lte", Comparator.THRESHOLD_LTE, "6000"),
        _tag("btc-8k", "BTC-USD-dec31-lte", Comparator.THRESHOLD_LTE, "8000"),
    ]
    rels = generate_ladder_relations(tags)
    assert len(rels) == 2
    # (4k, 6k) → antecedent=4k (lower/stronger), consequent=6k
    assert rels[0] == Relation("btc-4k", "btc-6k", "BTC-USD-dec31-lte ladder: 4000 ⇒ 6000")
    # (6k, 8k) → antecedent=6k (lower/stronger), consequent=8k
    assert rels[1] == Relation("btc-6k", "btc-8k", "BTC-USD-dec31-lte ladder: 6000 ⇒ 8000")


# ---------------------------------------------------------------------------
# Fingerprint gate
# ---------------------------------------------------------------------------


def test_mixed_fingerprints_no_cross_fingerprint_relation() -> None:
    """Two fingerprint cohorts in one underlying+comparator group never cross-ladder."""
    tags = [
        _tag("a1", "BTC-touch", Comparator.BY_DATE, "2026-06-30", fingerprint="coinbase:close-utc"),
        _tag("a2", "BTC-touch", Comparator.BY_DATE, "2026-12-31", fingerprint="coinbase:close-utc"),
        _tag("b1", "BTC-touch", Comparator.BY_DATE, "2026-06-30", fingerprint="binance:close-utc"),
        _tag("b2", "BTC-touch", Comparator.BY_DATE, "2026-12-31", fingerprint="binance:close-utc"),
    ]
    rels = generate_ladder_relations(tags)
    assert len(rels) == 2  # one per fingerprint cohort
    pairs = {(r.antecedent_condition_id, r.consequent_condition_id) for r in rels}
    # Same-fingerprint pairs present
    assert ("a1", "a2") in pairs
    assert ("b1", "b2") in pairs
    # Cross-fingerprint pairs absent
    assert ("a1", "b2") not in pairs
    assert ("b1", "a2") not in pairs


# ---------------------------------------------------------------------------
# Different underlying_key — never ladders together
# ---------------------------------------------------------------------------


def test_different_underlying_key_never_ladders_together() -> None:
    """Markets on different underlying_keys must never produce a shared relation."""
    tags = [
        _tag("eth-jun", "ETH-USD", Comparator.BY_DATE, "2026-06-30"),
        _tag("eth-dec", "ETH-USD", Comparator.BY_DATE, "2026-12-31"),
        _tag("btc-jun", "BTC-USD", Comparator.BY_DATE, "2026-06-30"),
    ]
    rels = generate_ladder_relations(tags)
    # Only the ETH pair ladders (BTC has a single market, not enough to ladder)
    assert len(rels) == 1
    assert rels[0].antecedent_condition_id == "eth-jun"
    assert rels[0].consequent_condition_id == "eth-dec"
    condition_ids = {
        cid for r in rels for cid in (r.antecedent_condition_id, r.consequent_condition_id)
    }
    assert "btc-jun" not in condition_ids


# ---------------------------------------------------------------------------
# transitive_closure — SPORTS_NESTING
# ---------------------------------------------------------------------------


def test_transitive_closure_sports_contains_expected_pairs() -> None:
    """Transitive closure includes all direct and transitive implications."""
    closure = transitive_closure(SPORTS_NESTING)
    # Direct edges
    assert ("win_championship", "reach_final") in closure
    assert ("reach_final", "make_playoffs") in closure
    assert ("win_division", "make_playoffs") in closure
    # Transitive: win_championship → reach_final → make_playoffs
    assert ("win_championship", "make_playoffs") in closure


def test_transitive_closure_sports_excludes_invalid_edges() -> None:
    """§5 invalid / incomparable pairs must not appear in the closure."""
    closure = transitive_closure(SPORTS_NESTING)
    # win_championship → win_division: invalid — a wildcard team can win without the division
    assert ("win_championship", "win_division") not in closure
    # win_division ↔ reach_final: incomparable — a division winner can lose round-1;
    # a wildcard can reach the final
    assert ("win_division", "reach_final") not in closure
    assert ("reach_final", "win_division") not in closure


def test_transitive_closure_no_self_pairs() -> None:
    """No node should appear as both specific and general in the same pair."""
    closure = transitive_closure(SPORTS_NESTING)
    for specific, general in closure:
        assert specific != general


def test_transitive_closure_politics_contains_expected_pairs() -> None:
    """Political chain: win_presidency → is_candidate via transitive closure."""
    closure = transitive_closure(POLITICS_NESTING)
    assert ("win_presidency", "win_party_nomination") in closure
    assert ("win_party_nomination", "is_candidate") in closure
    assert ("win_presidency", "is_candidate") in closure


# ---------------------------------------------------------------------------
# generate_dag_relations
# ---------------------------------------------------------------------------


def test_dag_relations_maps_nodes_to_conditions() -> None:
    """Node ids are resolved to condition_ids; closure drives which pairs are emitted."""
    tags = [
        _nesting_tag("0xCHAMP", "NBA-2026-LAL", "win_championship"),
        _nesting_tag("0xFINAL", "NBA-2026-LAL", "reach_final"),
        _nesting_tag("0xPLAY", "NBA-2026-LAL", "make_playoffs"),
        _nesting_tag("0xDIV", "NBA-2026-LAL", "win_division"),
    ]
    rels = generate_dag_relations(tags, SPORTS_NESTING)
    pairs = {(r.antecedent_condition_id, r.consequent_condition_id) for r in rels}
    # Direct edges
    assert ("0xCHAMP", "0xFINAL") in pairs
    assert ("0xFINAL", "0xPLAY") in pairs
    assert ("0xDIV", "0xPLAY") in pairs
    # Transitive: win_championship → make_playoffs
    assert ("0xCHAMP", "0xPLAY") in pairs
    # Invalid / incomparable pairs must not appear
    assert ("0xCHAMP", "0xDIV") not in pairs
    assert ("0xDIV", "0xFINAL") not in pairs


def test_dag_relations_missing_node_skipped() -> None:
    """If one end of a closure pair has no live market, that pair is silently skipped."""
    # Only championship and playoffs — no reach_final, no win_division
    tags = [
        _nesting_tag("0xCHAMP", "NBA-2026-LAL", "win_championship"),
        _nesting_tag("0xPLAY", "NBA-2026-LAL", "make_playoffs"),
    ]
    rels = generate_dag_relations(tags, SPORTS_NESTING)
    pairs = {(r.antecedent_condition_id, r.consequent_condition_id) for r in rels}
    # Transitive pair is emitted because both nodes are present
    assert ("0xCHAMP", "0xPLAY") in pairs
    # reach_final and win_division are absent from the market set — their condition ids
    # must not appear in any relation
    all_cids = {cid for pair in pairs for cid in pair}
    assert all_cids == {"0xCHAMP", "0xPLAY"}


def test_dag_relations_fingerprint_gate() -> None:
    """Two markets with differing resolution_fingerprint must not form a relation."""
    tags = [
        _nesting_tag("0xCHAMP", "NBA-2026-LAL", "win_championship", fingerprint="fp-A"),
        _nesting_tag("0xPLAY", "NBA-2026-LAL", "make_playoffs", fingerprint="fp-B"),
    ]
    rels = generate_dag_relations(tags, SPORTS_NESTING)
    assert rels == []


def test_dag_relations_different_underlyings_isolated() -> None:
    """Markets from different underlying_keys never pair, even if node ids match."""
    tags = [
        _nesting_tag("0xLAL-CHAMP", "NBA-2026-LAL", "win_championship"),
        _nesting_tag("0xGSW-PLAY", "NBA-2026-GSW", "make_playoffs"),
    ]
    rels = generate_dag_relations(tags, SPORTS_NESTING)
    assert rels == []
