# Logical-Dependency & Monotonicity Relations

> Specification for `src/polyarb/resolution/relations.py` and `src/polyarb/detectors/dependency.py`.
> Notation follows `SPEC.md`: `a_yes`, `a_no` are best **ask** prices (cost to buy a share),
> `b_yes`, `b_no` are best **bids**, `price(X)` is the effective/mid price of YES on market `X`,
> `f` = total taker fees for the legs, `g` = gas estimate per round trip. All prices in `[0, 1]`.

These pairs are **candidates to verify**, not guaranteed money. Liquid, obvious edges get arbed fast; the value is in long-dated rungs, extreme thresholds, and deep-run sports markets. Every emitted opportunity still carries resolution risk and must clear the fingerprint gate in §6.

---

## 1. Sign convention — the one rule everything hangs on

For an implication **A ⇒ B** ("A being true forces B true"), the more specific / harder event `A` must trade **at or below** the more general / weaker event `B`:

```
A ⇒ B   ⟹   price(A) ≤ price(B)
```

A **violation** — the only thing worth flagging — is the specific leg trading **richer** than the general leg:

```
violation:   price(A) > price(B)
```

**Locked trade:** buy `YES_B` and buy `NO_A`.

> Canonical identity: SPEC.md §The math; reproduced here for the ladder example.

| Resolved outcome | `YES_B` pays | `NO_A` pays | Total payoff |
|---|---|---|---|
| A occurs (⇒ B occurs) | 1 | 0 | **1** |
| B occurs, not A | 1 | 1 | 2 |
| neither occurs | 0 | 1 | **1** |

Minimum payoff is `1`. Cost is `a_yes,B + a_no,A`. Locked profit:

```
net_profit ≥ 1 − (a_yes,B + a_no,A) − f − g  ≈  price(A) − price(B) − f − g
```

Realizes **at resolution**, so also compute `annualized = (net_profit / cost) * (365 / days_to_resolution)`.

> Direction discipline: store every edge as `specific → general`. Tag the **specific** leg as the higher `nesting_level` is wrong — *general* is the higher level (it sits above). Keep columns labelled "specific (stronger, should be cheaper)" vs "general (weaker, should be richer)" everywhere to kill ambiguity.

---

## 2. Two relation mechanisms — keep them apart in code

### 2a. Total-order ladders (auto-generated)
Dates and numeric thresholds form a chain. **Do not hand-list pairs.** Tag each market with a comparable bound, sort, and let edges generate themselves. Monotonicity is transitive, so after sorting you only check **adjacent rungs** — that catches every violation in the ladder in `O(n)`, not `O(n²)`.

- *By-date ladder:* sort ascending by deadline → YES prices must be **non-decreasing**.
- *Threshold-`≥` ladder:* sort ascending by threshold → YES prices must be **non-increasing**.
- *Threshold-`≤` ladder:* sort ascending by threshold → YES prices must be **non-decreasing**.

### 2b. Declared nesting DAGs (hand-curated)
Sports rounds and political stages form a **partial** order, not a chain. Declare the explicit `⇒` edges and take the **transitive closure**. Never auto-generate these from a linear rank — that is the classic way to ship a money-losing false edge (see §3, §4, §5).

---

## 3. Tag schema

Every market needs these five tags so ladders sort themselves and DAG nodes resolve:

| Tag | Type | Purpose | Example |
|---|---|---|---|
| `underlying_key` | str | Canonical subject; only same-key markets compare | `BTC-USD`, `fed-cuts-2026`, `NBA-2026-LAL`, `us-pres-2028-CANDIDATE` |
| `comparator` | enum | `by_date` / `threshold_gte` / `threshold_lte` / `nesting` / `window` | `threshold_gte` |
| `bound` | date \| float \| node | The deadline, numeric threshold, or DAG node id | `2026-12-31`, `150000`, `make_playoffs` |
| `comparator_kind` | enum | `cumulative_touch` vs `point_in_time` — **only the former ladders** | `cumulative_touch` |
| `resolution_fingerprint` | str/hash | Settlement source + cutoff + timezone + index, for the §6 gate | `coinbase-spot:close-utc` |

---

## 4. Seed relations, in scan-priority order

### Priority 1 — Crypto price ladders *(scan first)*
Most abundant, objective resolution (a price feed; near-zero UMA dispute risk), and the under-arbed edges live in the longer-dated and extreme-threshold tails. Two ladder types on the same underlying:

- **Temporal (touch):** fixed threshold across dates.
  `"BTC reaches $150k by June" ⇒ "BTC reaches $150k by EOY"` → `price(earlier) ≤ price(later)`.
- **Threshold (fixed date):** `≥ X` on one date across thresholds.
  `"ETH ≥ $10k on Dec 31" ⇒ "ETH ≥ $6k on Dec 31"` → ascending threshold ⇒ non-increasing price.

**Trap:** only **cumulative / touch** markets ("reaches X at any point by T") join the temporal ladder; only fixed-date `≥`/`≤` markets join the threshold ladder. A **point-in-time** "close *above* X *on* date T" market is a different instrument — never mix it into either ladder. This is enforced by `comparator_kind`.

> *Worked example.* On `ETH-USD`, fixed date Dec 31, `comparator=threshold_gte`: `≥$4k @ 0.71`, `≥$6k @ 0.55`, `≥$8k @ 0.40`, `≥$10k @ 0.42`. Sorted ascending by threshold the prices must be non-increasing; `0.40 → 0.42` breaks it. Flag the `(≥$8k, ≥$10k)` rung: buy `YES(≥$8k)` + `NO(≥$10k)`.

### Priority 2 — Temporal by-date event ladders
Any one-time event listed across multiple deadlines. Abundant, mostly objective.

- **Fed rate cuts, *cumulative* framing:** `"≥1 cut by March meeting" ⇒ "≥1 cut by June meeting" ⇒ "≥1 cut in 2026"`.
  Do **not** ladder the *point-in-time* "cuts *at* the March meeting" markets, and do **not** ladder the *exactly-N-cuts* set — that mutually-exclusive set is the NegRisk **basket** detector's job, not this one.
- **Announcement / launch / resignation ladders:** `"[X] declares candidacy by [date]"`, `"[company] IPOs by [date]"`, `"[product] ships by [date]"` across a date series.
- **Window disjunction** (special temporal case): a narrow window implies its container.
  `"shutdown in March 2026" ⇒ "shutdown in 2026"`; `"recession declared in Q2" ⇒ "recession in 2026"` → `price(narrow) ≤ price(broad)`. Tagged `comparator=window`.

### Priority 3 — Sports round nesting *(declared DAG)*
Recurs every season across every league; fully objective (game results); deep-run markets (champion, reach-final) are chronically under-priced relative to make-playoffs. Per team, per `underlying_key`:

| Specific (stronger, should be cheaper) | ⇒ | General (weaker, should be richer) | Holds because |
|---|---|---|---|
| `win_championship` | ⇒ | `reach_final` | can't win it without reaching the final |
| `reach_final` | ⇒ | `make_playoffs` | can't reach the final without making the bracket |
| `win_division` | ⇒ | `make_playoffs` | division winners qualify |
| `win_championship` | ⇒ | `make_playoffs` | transitive closure of the above |

(For NBA/NFL phrasing, `win_conference ≈ reach_final`.) Applies to bracketed leagues — NBA, NFL, NHL, MLB, UEFA Champions League, World Cup.

**INVALID edges — never add these:**
- `win_championship → win_division` ✗ — a wildcard can win it all without winning its division.
- `reach_final ↔ win_division` ✗ — a division winner can lose round one.
- `win_division` and `reach_final` are **both** below `make_playoffs` but **incomparable to each other**. Leave no edge between them.

### Priority 4 — Political stage nesting *(declared chain — lower priority, higher resolution-risk)*
```
win_presidency ⇒ win_party_nomination ⇒ is_candidate / runs
(transitive: win_presidency ⇒ runs)
```
Genuinely exploitable cases exist, but politics is the most-watched, most-arbed, and most dispute-prone category — tag every node here `resolution_risk = elevated` and rank it below crypto/sports.

**Caveat:** `win_presidency ⇒ win_party_nomination` holds **only** for candidates whose sole viable path is through that nomination. An independent / third-party path breaks it. Restrict this edge to declared major-party candidates, or drop its confidence weight.

### Priority 5 — Conjunction decomposition *(opportunistic, rare)*
When component markets exist separately:
```
"A and B both happen" ⇒ "A happens"   (and ⇒ "B happens")
price(A∧B) ≤ min(price(A), price(B))
```
Compound markets are uncommon on Polymarket — wire the rule, expect few hits.

---

## 5. Exclusions — look like pairs, are **not** lockable

- **Correlated-but-not-nested pairs** (e.g. *win-a-state* vs *win-the-presidency*). Winning Pennsylvania neither implies nor is implied by winning the presidency. Treating the gap as an edge is a **forecasting bet**, not a locked arb — exactly what this project avoids. HFT writeups call these a "mismatch signal"; that signal is soft. Exclude.
- **Point-in-time mixed into a cumulative ladder** — the single most common implementation bug here. Guarded by `comparator_kind`.
- **Mismatched resolution criteria** on two seemingly-nested markets (different price index, settlement source, or cutoff timezone). See §6.

---

## 6. The resolution-fingerprint gate

Two markets only form a valid implication if they resolve off the **same underlying fact under the same rules**. Before emitting any dependency opportunity, require:

```
A.resolution_fingerprint == B.resolution_fingerprint   (for the shared underlying)
```

The fingerprint captures settlement source, reference index/feed, cutoff time, and timezone. If the fine print differs — one BTC market resolves on Coinbase spot at UTC close, the other on a different index or a different cutoff — the logical relationship can break at the edges and the "arb" becomes a bet on the discrepancy. Mismatch ⇒ do not emit (or emit only as a tagged, non-executable research note).

---

## 7. How relations feed the scan

1. `gamma` discovery tags each market with the §3 schema (`underlying_key`, `comparator`, `bound`, `comparator_kind`, `resolution_fingerprint`).
2. `resolution/relations.py` produces edges by two paths:
   - **Ladder generator:** group by `underlying_key` + `comparator`, drop non-`cumulative_touch` where required, sort by `bound`, emit adjacent rungs.
   - **DAG generator:** load the declared sports/political edge sets, compute transitive closure, match nodes to live markets by `underlying_key` + `bound`.
3. `detectors/dependency.py` checks each edge for the §1 violation `price(A) > price(B)`, pulls live books, and (via `pricing/`) computes net-of-fee profit and executable size.
4. The §6 fingerprint gate is enforced inside `resolution/relations.py` at generation time for ladder- and DAG-generated relations; hand-declared `add_relation()` relations do not yet enforce it (tracked: STRATEGY_BACKLOG D1). `engine/filters.py` applies the resolution-risk gate, the min-profit / min-notional thresholds, and dedupe before ranking.

Start the live scan with **Priority 1 and 2 only**, confirm the false-positive rate is ~zero on recorded fixtures, then enable the Priority 3 DAG, and only then Priority 4.
