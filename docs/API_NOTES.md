# API_NOTES.md — verified Polymarket API facts

**Verified:** 2026-06-28 by reading the live docs. The rest of the build references this
file, not assumptions. Re-verify before changing any client. Sources read this round:

- https://docs.polymarket.com and https://docs.polymarket.com/llms.txt (doc index)
- https://docs.polymarket.com/api-reference/rate-limits.md
- https://docs.polymarket.com/trading/fees.md
- https://docs.polymarket.com/advanced/neg-risk.md
- https://docs.polymarket.com/concepts/pusd.md
- https://docs.polymarket.com/api-reference/wss/market.md
- https://github.com/Polymarket/py-clob-client and https://github.com/Polymarket/py-sdk

---

## Base URLs

| Service   | Base URL                                              | Purpose                                              | Auth for reads |
|-----------|-------------------------------------------------------|------------------------------------------------------|----------------|
| Gamma     | `https://gamma-api.polymarket.com`                    | Discovery: events, markets, negRisk flags, metadata  | None           |
| CLOB      | `https://clob.polymarket.com`                         | Order books, prices, midpoints, fees, tick/min size  | None for reads |
| Data      | `https://data-api.polymarket.com`                     | Positions, trades, holders, PNL                      | None           |
| WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market`| Live public orderbook & price updates                | None (market)  |

CLOB **trading** (POST/DELETE order) requires API credentials + a signing client.
WebSocket **user** channel (`/ws/user`) requires auth. The detector touches none of these.

## Rate limits (per service, 10s windows unless noted)

Docs say excess requests are **throttled/queued, not 429-rejected** — but still implement a
token bucket + backoff defensively (Cloudflare can still 429).

- **Gamma** — general 4,000/10s; `/events` 500/10s; `/markets` 300/10s; `/public-search`
  350/10s; `/comments`, `/tags` 200/10s each.
- **Data** — general 1,000/10s; `/trades` 200/10s; `/positions`, `/closed-positions`
  150/10s each.
- **CLOB** — general 9,000/10s; market data (`/book`, `/price`, `/midpoint`) 1,500/10s.
  Trading (not used by detector): `POST/DELETE /order` 5,000/10s burst, 120,000/10min
  sustained; `POST/DELETE /orders` 2,000/10s burst, 21,000/10min; `DELETE /cancel-all`
  250/10s burst, 6,000/10min.
- **WebSocket** — no concurrent-connection cap documented (do not assume unlimited;
  subscribe many assets on one connection rather than fanning out connections).
- Other: Bridge 50/10s; Relayer `/submit` 25/min; User PNL 200/10s.

## Settlement / collateral token — **pUSD** (not raw USDC)

- Current collateral is **pUSD** ("Polymarket USD"), a standard ERC-20 on **Polygon**,
  backed 1:1 by USDC. The protocol settles in native USDC underneath.
- Wrap (USDC.e → pUSD): `CollateralOnramp` at `0x93070a847efEf7F70739046A929D47a521F5B8ee`.
  Unwrap via `CollateralOfframp` (address not captured — re-fetch if needed for execution).
- For the detector this only matters for naming/units (treat 1 unit = 1 pUSD = 1 USD).
  Split/merge and NegRisk-convert operate on pUSD collateral. Relevant for Phase 5 only.

## Fees — taker-only, parabolic in price

- **Makers pay no fee. Taker fee only.** Formula:

  `fee = C × feeRate × p × (1 - p)`  where `C` = shares traded, `p` = share price.

  Fee is maximal at p=0.50 and → 0 at the extremes. Charged in USDC at match time; orders
  carry no fee field.
- **feeRate by category** (verify per-market, these are category defaults):
  Crypto 0.07 · Sports 0.03 · Finance/Politics/Tech/Mentions 0.04 ·
  Economics/Culture/Weather/Other 0.05 · **Geopolitics & world events 0 (fee-free)**.
- Per-market fee params come directly on the Gamma market object: `feesEnabled` (bool),
  `feeType` (e.g. `crypto_fees_v2`, `culture_fees`, `sports_fees_v2`, `general_fees`, or
  `null` for fee-free), and `feeSchedule` (object or null). Fee-free markets have
  `feesEnabled:false`, `feeType:null`, `feeSchedule:null`.
- **Full `feeSchedule` shape** (verified 2026-06-30 against live Gamma `/markets` across
  crypto/sports/culture/general categories — 20 markets sampled):

  ```json
  {
    "exponent": 1,
    "rate": 0.07,
    "takerOnly": true,
    "rebateRate": 0.25
  }
  ```

  `exponent: 1` confirms the formula is `p^1 × (1-p)^1` (pure parabolic, as implemented).
  `takerOnly: true` confirms makers pay zero. `rebateRate` is a maker-side rebate (does not
  affect taker fee). **No `min`, `floor`, `minimum`, or equivalent field exists.**
  Only `rate` is surfaced as `Market.fee_rate`; the others are informational.
- Computed fees are rounded to 5 dp; <0.00001 USDC rounds to zero.
- Implication for `MIN_PROFIT_BPS`: threshold should be lower for fee-free categories,
  higher for fee'd ones — derive per market from live params, per SPEC.

### M3-feefloor investigation (2026-06-30) — CLOSED: no floor found

Backlog item M3-feefloor raised concern that a per-order fee minimum would make the
parabolic model understate fees on longshot legs (p → 0/1), producing false positives.

**Verification method:** live read-only recon via `GET /markets?limit=20` on the Gamma API
(no auth, no order placement). Inspected `feesEnabled`, `feeType`, and the full `feeSchedule`
object for all 4 fee-type patterns present in the sample (crypto 0.07, sports 0.03, culture
0.05, general 0.05).

**Finding:** the `feeSchedule` object has exactly four fields (`exponent`, `rate`,
`takerOnly`, `rebateRate`). No `min`, `floor`, `minimum`, or any analogous field was present
in any sampled market. The formula `C·r·p·(1−p)` (with `exponent=1`) is the complete taker
cost; fee approaching zero at the price extremes is correct, not a modeling gap.

**Decision:** no code change to `taker_fee`. A pinning test (`test_m3_no_fee_floor_at_longshot_prices`
in `tests/test_fees.py`) locks the intentional behavior and serves as a trip-wire if Polymarket
later introduces a floor. Re-investigate if the `feeSchedule` schema gains new fields or if
Polymarket documentation explicitly describes a per-order minimum.

## NegRisk (multi-outcome, mutually-exclusive events)

- **Detect** via the event object: `negRisk: true`. Augmented (outcomes can be added
  later) shows `enableNegRisk: true` and `negRiskAugmented: true`.
- **Convert** (NegRisk Adapter contract): a **No** share in one market converts into **1
  Yes share in every other market** in the event, atomically. This is a *capital-efficiency*
  tool — pay 1, get 1, **no profit**. **Arb profit comes from buying the underpriced YES
  basket through the standard order books, not from convert.** (Encode this in code +
  docstring + test, per SPEC.)
- Standard markets are independent; negRisk links all markets in an event.
- **Merge of a single constituent's YES+NO → $1 is valid and FEE-FREE** (verified 2026-06-29
  against `NegRiskAdapter.sol`, Polymarket/neg-risk-ctf-adapter). The YES/NO of one NegRisk
  constituent *are* genuine CTF complementary pairs; `NegRiskAdapter.mergePositions` returns
  exactly the merged `amount` in USDC with **no protocol fee** — the `feeBips` charge applies
  only to `convertPositions`, not to merge. **Consequence: the complement-arb cost model
  `profit = 1 − cost − fees − gas` is correct for NegRisk constituents; do NOT exclude them.**
  Phase-5 (execution) caveats only: the merge must call `NegRiskAdapter.mergePositions`
  (Polygon `0x59c8b7221766b8f06c8484d9b679fa0ac72050d7`), **not** the CTF directly, and it
  costs slightly more gas (an extra internal `wcol.unwrap`) — a per-execution gas model should
  use a higher constant for `negRisk` markets. Merge is still a single, instant tx (no
  resolution wait).

## Identifier model (RESOLVED in Phase 1 — verified against fixtures)

- Event-level id: `id`. NegRisk flag (`negRisk`) lives on the event AND each sub-market;
  `negRiskMarketID` is shared across all sub-markets of a negRisk event.
- A Gamma **market** carries `conditionId` (on-chain market key) and `clobTokenIds` — a
  **JSON-encoded string** of `[YES_token_id, NO_token_id]`, index-aligned with `outcomes`
  (`outcomes[0]`="Yes" → `clobTokenIds[0]`=YES token). `outcomePrices` is encoded the same
  way. The CLOB book echoes `market` (=conditionId) and `asset_id` (=token_id).
- A token_id is the **order/quote identifier** (a.k.a. `asset_id`), one per outcome side.
- Quirks the models normalize (see `src/polyarb/models.py`): JSON-string list fields are
  decoded; book `bids`/`asks` come sorted worst→best so best bid/ask are taken by value;
  `feeSchedule.rate` → `fee_rate`; some markets omit `clobTokenIds` (not yet tradeable) and
  some token_ids 404 on `/book`.

- **`/markets?closed=true&condition_ids=...` (E1 settle poller — VERIFIED LIVE 2026-07-01):** the
  read-only `polyarb settle` poller (`GammaClient.resolved_markets`) fetches resolved markets by
  `condition_ids`. Confirmed: the `condition_ids` filter + `active=false`/`closed=true` pairing
  works — asked for N condition_ids, got exactly those N back as `closed=true`.
- **Resolved `outcomePrices` are NOT exact 0/1 (VERIFIED LIVE 2026-07-01 — corrected an E1 bug):**
  a resolved binary's `outcomePrices` is the last-mid at close, converging *near* the payout but
  not to it — winners come back **~0.9999** (seen down to ~0.9664), losers **~1e-6**; a genuine
  50-50 void sits near **0.5**. So settlement must **round to the nearest payout {0, 0.5, 1}**, not
  test exact equality (the original `settle` flagged every real resolution as a "void"). `settle`
  now rounds with a middle void band [0.25, 0.75]. Limitation: a legitimately-resolved market whose
  last-mid landed in that band is conservatively treated as a void (triggers an E2 review alert,
  never a silently-wrong P&L). A rare `[0,0]`/malformed payload rounds both legs to 0.

## WebSocket market channel

- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`.
- Subscribe: `{"assets_ids": ["<token_id>", ...], "type": "market"}`. Optional:
  `initial_dump` (bool, default true — snapshot on subscribe), `level` (1–3, default 2),
  `custom_feature_enabled` (default false).
- Dynamic (re)subscription without reconnect:
  `{"operation": "subscribe"|"unsubscribe", "assets_ids": ["<token_id>"]}`.
- **Initial-dump frame size (live-verified 2026-07-01):** the `initial_dump` snapshot is sent as a
  **single frame** whose size scales with the number of subscribed tokens — measured **~1.65 MiB
  for ~390 tokens** (~4.3 KiB/token), so the 600-market default (~1200 tokens) is several MiB. The
  `websockets` Python library caps inbound frames at **1 MiB** by default and closes the connection
  immediately with **1009 MESSAGE_TOO_BIG** — the feed then never delivers a message and a runner
  silently rides on the REST-resync backup. Set `websockets.connect(max_size=...)` generously (we
  use 64 MiB, config `WS_MAX_MESSAGE_BYTES`). Deltas (`price_change`) are small; only the initial
  dump is large.

## ⚠️ SDK CHANGE — affects Phase 5 (execution) only

- **`py-clob-client` is ARCHIVED and "no longer functional"** — the README explicitly says
  do not use it for new or existing integrations. **SPEC.md names py-clob-client; that is
  now stale.**
- Replacement: the new unified SDK **`py-sdk`** (https://github.com/Polymarket/py-sdk),
  pip package **`polymarket-client`** (beta — install with `pip install --pre
  polymarket-client`). Exposes `PublicClient` / `AsyncPublicClient` (e.g. `get_market(...)`)
  and presumably the signing/trading client for execution. Python version unconfirmed.
- **Decision needed before Phase 5:** swap the execution module to `polymarket-client`.
  Detection (Phases 1–4) uses `httpx` directly against the REST endpoints above and does
  **not** depend on either SDK, so this does not block the detector.

## Public read methods confirmed (archived py-clob-client, for reference)

`get_ok`, `get_server_time`, `get_midpoint(token_id)`, `get_price(token_id, side)`,
`get_order_book(token_id)`, `get_order_books([BookParams(...)])`, `get_simplified_markets()`
— all no-auth. We will call the equivalent REST endpoints directly via `httpx` rather than
depend on the archived client.

## Gas — who pays, and our config defaults (verified 2026-06-30)

**Verdict: on the relayer path, Polymarket pays gas; true user cost ≈ $0.** Polymarket uses a
GSN-style meta-transaction relayer: the user signs locally, the relayer broadcasts and **pays
the Polygon gas** (docs.polymarket.com/trading/gasless). Gas responsibility by wallet:

| Wallet type | Gas payer |
|---|---|
| Proxy (Magic/Google login) | **Relayer (Polymarket)** |
| Safe (Gnosis) | **Relayer** |
| Deposit wallet (API V2 default) | **Relayer** |
| Raw EOA (bare private key) | **User pays** |

- **CLOB orders**: placement/cancel are off-chain signed messages (no gas, always). Fill
  *settlement* (`matchOrders`) is relayer-paid for proxy/Safe/deposit wallets.
- **CTF split / merge / redeem** (incl. NegRisk merge/redeem): explicitly relayer-covered
  ("CTF operations: Split, merge, and redeem positions" — docs.polymarket.com/trading/gasless;
  github.com/Polymarket/agent-skills gasless.md).

**Config defaults (`config.py`):** `gas_estimate=0.02`, `gas_per_leg_estimate=0.05` USDC — a
deliberately conservative *ceiling* (true relayer cost ≈ $0), set non-zero only to cover the
raw-EOA / undocumented relayer-cap edge. Derivation (conservative): gas units from real
Polygonscan receipts — CTF merge ~125–143k, redeem ~170–192k, NegRisk redeem ~208k, exchange
`matchOrders` 380–670k — at a ceiling 600 gwei (historical floor ~25–30; mid-2026 congested at
245–500) and POL ≈ $0.10 ⇒ ~$0.015 fixed (250k gas) and ~$0.042/leg (700k gas), rounded up.
**If the executor is confirmed relayer-only, set both ~0.**

**Live (dynamic) gas — keyless endpoints for the future `use_dynamic_gas` client:**
- Polygon gas price (gwei): `GET https://gasstation.polygon.technology/v2` → use `standard.maxFee`
  (or `fast.maxFee`); poll ≤ 1/block (~2s). (The old polygonscan oracle is dead → Etherscan v2,
  which now needs a key; prefer the Gas Station.)
- POL/USD: `GET https://api.coingecko.com/api/v3/simple/price?ids=polygon-ecosystem-token&vs_currencies=usd`
  → `["polygon-ecosystem-token"]["usd"]`. (The old `matic-network` id is deprecated → `{}`.)

**Unconfirmed / source later:** relayer daily gas-subsidy cap (exists, $ figure unpublished —
ask Polymarket / Builder Program); NegRisk `mergePositions`/`convertPositions` gas (no recent
on-chain sample — capture a receipt if executed); EOA per-leg attribution (`matchOrders`
settles the whole match, so 0.05/leg over-counts — refine against the real execution path).
