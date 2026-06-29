# Hedging design — when you can't buy the whole basket

> Status: **design doc, pre-implementation.** Drafted 2026-06-29 after the basket
> exhaustiveness work (A1). The model-free pieces (drop-eliminated, the NO-dual) are
> implementable now; the probabilistic piece (§5) is a **product-principle decision for
> Jonathan** — it is NOT built and must not ship by default.

## The problem

The NegRisk basket arb buys 1 YES of every outcome of an exhaustive, mutually-exclusive event
for `Σ a_yes,i < 1`; exactly one resolves to \$1, so the payoff is a guaranteed \$1 and the
edge is `1 − Σ − fees − gas`, **model-free** (no forecasting opinion — SPEC §Mission).

That guarantee is only as good as "we hold YES of *every* possible winner." In practice you
often **can't or shouldn't buy all the legs**:

1. **Eliminated outcomes** — some constituents have already resolved NO (e.g. knocked-out
   teams). Their books are gone.
2. **Illiquid / missing legs** — a live outcome has no book or no ask; you can't lock it.
3. **Edge erosion with depth** — buying every leg deep enough raises the combined VWAP past
   \$1; only a small size is actually profitable.
4. **Capital / N** — a 60-outcome event needs 60 simultaneous fills; capital or operational
   limits make the full set impractical.

If you respond by buying only a **subset** `S ⊊ {1..N}`, you no longer hold an arb: you hold a
**directional bet** that pays \$1 if the winner ∈ S and \$0 otherwise. The question Jonathan
raised: *how do we hedge that?*

## The hard fact: a partial basket cannot be made riskless model-free

To guarantee a payoff floor in **every** outcome you need positive payoff in the outcomes of
`Sᶜ` too. By linear-programming / no-arbitrage duality, the only **model-free** payoff floors
available from `{YES_j, NO_j}` are the *complete* structural identities — the full YES basket
(`Σ YES < 1`), each per-market complement (`YES_j + NO_j < 1`), and the full NO basket
(`Σ NO < N−1`). (NO legs of `Sᶜ` can cover *each other* — e.g. with `Sᶜ={3,4}`, `NO₃` pays on
outcome 4 and `NO₄` on outcome 3 — but any strict subset leaving ≥2 outcomes uncovered still
costs at least its own floor; there is no cheaper synthetic floor.) So **"hedging a partial
basket to riskless" is identical to "completing the basket."** Anything short of completion
carries irreducible outcome risk that can only be priced with a probability view — which the
product forbids.

### Convert does not help (verified)

The NegRisk `convertPositions` operation burns `NO` tokens of a chosen subset and mints `YES`
of the complement + `(k−1)` collateral, with `feeBips` taken from the output (per
`docs/API_NOTES.md` / `NegRiskAdapter.sol`). It is a **capital-efficiency** tool, not a
completion tool: to fill a missing leg via convert you must first buy NO legs, and the result
is a fee-bearing tangle of duplicate YES positions, never a clean completion. We model its
arbitrage P&L as zero (`negrisk_convert_pnl ≡ 0`) — and the `feeBips` on its output make it
*slightly negative* in practice, which only strengthens "convert doesn't help." It stays out
of the hedging path.

## The model-free menu (what we DO)

When the full YES basket is infeasible or edge-eroding, these stay inside the "structural,
model-free" line:

### 1. Drop eliminated outcomes — **done (A1)**
A `closed` constituent that **resolved NO** (verified via its resolved YES price ≈ 0) is out of
contention; exactly one of the *live* outcomes wins, so the basket over just the live legs is
still a complete exhaustive partition and pays \$1. The detector drops such legs and emits over
the remainder. Critically, a closed leg that **won** (YES ≈ 1) — the winner closes *first*, with
the losers' books going stale-cheap — must NOT be dropped: that would build a basket of
guaranteed losers paying \$0 (and it would rank top, since the fake edge is huge). So a closed
leg whose resolution can't be proven a loss (won, void, or unknown price) → skip the whole
event. The detector also skips if any *live* leg is a hole (not tradeable / no book / crossed),
since then the partition can't be proven complete. This recovers the most common "can't buy
all" case (eliminations) while refusing the catastrophic one. See
`detectors/negrisk_basket.py`, STRATEGY_BACKLOG A1.

### 2. The NO-basket dual (`Σ NO < M − 1`) — **next to implement (B3)**
Over `M` live outcomes, exactly `M−1` resolve NO, so **buying 1 NO of every live outcome pays
a guaranteed `M−1`**. Arb if `Σ_live a_no,i < M − 1 − fees − gas`. This is the structural
hedge for *feasibility*: when the YES side is thin or edge-eroded, the NO side may have depth
elsewhere. It is a *different trade*, not a directional slice: still a guaranteed payoff, still
model-free. Realizes at resolution.

**Precondition is weaker than A1's.** The `M−1` *floor* needs only **mutual exclusivity**
(≤1 YES among the live legs), which NegRisk guarantees by construction — so B3 must **not**
inherit A1's augmented-skip or closed-YES guards: even if a dropped leg actually won, the NO
basket pays `M ≥ M−1` and the floor holds. (Full exhaustiveness would only tighten "≥ M−1" to
"exactly M−1", which the arb doesn't require.) Reuse A1's *tradeability/hole* filtering, not
its exhaustiveness gates.

**Caveats (execution, from the committee).** The NO-dual is a **coverage** tool, not a value
leader: it deploys ≈`M−1` dollars for the *same absolute* edge as the YES basket's ≈`<$1`, so
its return-on-capital is ~`1/(M−1)` of the YES side and it ranks far lower on `bps` — exactly
when `M` is large (the case it's meant for). Its edge is also a sum of tiny per-leg slacks
(NO-of-longshot ≈ 0.99, one tick from vanishing) and its liquidity advantage is *asserted, not
measured*. Treat it as feasibility coverage; rank it on the same depth-walk/gas basis and
expect it to lose to a feasible YES basket.

### 3. Refuse — **done (A1)**
A partial set that is neither completable nor dual-capturable is not emitted. Reporting it as
an arb would be fabricating profit. Silence is correct.

## §5 — The probabilistic partial basket (DECISION REQUIRED, not built)

The only way to "act on" a genuinely un-completable subset is to treat it as a bet and price
the residual. The least-bad, still-disciplined version uses the **market's own** implied
probabilities — but they must be used on a *consistent* (normalized) scale. With
`T = Σ_all a_yes,j`, the implied win-probability of holding subset `S` is `p = Σ_S / T`
(NOT `Σ_S` raw, and the loss probability is `1 − p`, not `Σ_{Sᶜ}`). Then, for cost `Σ_S`:

    EV(partial) = p·$1 − Σ_S = (Σ_S / T)·1 − Σ_S = Σ_S·(1 − T)/T   (≈ Σ_S·(1−T) for T≈1)

i.e. the partial position earns only its **pro-rata share** `Σ_S/T` of the event's slack
`1−T` — NOT the whole slack. (Using the un-normalized `Σ_S`/`Σ_{Sᶜ}` mixes two probability
scales and over-ranks partials — the EV would collapse to `1−T` independent of `S`, which is
wrong.) Rank/gate on `p·edge − (1−p)·loss` with this normalized `p`, edge `= 1−Σ_S`, loss
`= Σ_S`, and surface worst-case loss = cost. **And** this EV is an *optimistic* estimate: the
legs you drop are disproportionately the illiquid/no-ask ones, whose true probability exceeds
their stale printed price (adverse selection / Glosten-Milgrom) — so price the residual
conservatively (e.g. the NO-ask cost to actually cover `Sᶜ`) and treat the EV as a lower bound.
Ties into ranking item C2.

**This crosses the SPEC "no forecasting opinion" boundary** — even market-implied, it's a
probabilistic bet, not a structural identity.

> **Decision (2026-06-29, Jonathan): build it — option (B), opt-in.** A separate, clearly-
> labelled "directional / not structural" opportunity class behind a config flag, **off by
> default**, never on the default `scan` path, ranked by the conservative market-implied EV
> above (`p = Σ_S/T`, residual priced at NO-ask cost, treated as a lower bound), carrying a
> distinct risk tag. The default product stays a pure structural-arb scanner; this is an
> additive, opt-in mode. **Not yet implemented** — sequenced after the structural correctness
> work (A2/A3) and the NO-dual.

## Build order

1. **A2 / A3 basket correctness** (void/50-50 handling, staleness/time-skew gate) — the
   remaining structural "guaranteed $1 can be $0" risks. *(In progress.)*
2. **NO-dual (§2 / B3)** — model-free coverage tool; reuses `live_partition(skip_augmented=
   False)`. Property-test the `M−1` payoff identity.
3. **Probabilistic partial basket (§5, opt-in)** — per the decision above; off by default,
   separate class, conservative lower-bound EV.
