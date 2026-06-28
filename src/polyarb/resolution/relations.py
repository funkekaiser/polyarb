"""Declared logical-dependency relations between markets.

A relation ``A ⇒ B`` asserts that whenever A resolves YES, B must too — so the identity
``P(A) ≤ P(B)`` must hold. The dependency detector flags violations (``price(A) > price(B)``)
and prices the locked trade *buy YES_B + buy NO_A*.

Dependencies are **declared, never inferred from text** (SPEC constraint). Relations are
hand-curated and keyed by on-chain ``condition_id``; adding one is a one-liner via
:func:`add_relation`. The seed list ships empty because condition_ids are
market-instance-specific — populate it for the live markets you're tracking.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Relation:
    """``antecedent ⇒ consequent`` (A ⇒ B), so ``P(A) ≤ P(B)`` must hold.

    ``antecedent_condition_id`` / ``consequent_condition_id`` are on-chain market ids.
    Examples of valid relations: "by date X" ⇒ "by date Y" for X earlier than Y;
    wins-presidency ⇒ wins-nomination; wins-championship ⇒ makes-playoffs.
    """

    antecedent_condition_id: str  # A
    consequent_condition_id: str  # B
    description: str


# Hand-curated seed graph. Populate with real condition_ids for the markets you track, e.g.:
#   add_relation("0x<presidency>", "0x<nomination>", "wins presidency ⇒ wins nomination")
SEED_RELATIONS: list[Relation] = []


def add_relation(antecedent: str, consequent: str, description: str) -> Relation:
    """Declare and register a relation in the seed graph; returns it."""
    relation = Relation(antecedent, consequent, description)
    SEED_RELATIONS.append(relation)
    return relation
