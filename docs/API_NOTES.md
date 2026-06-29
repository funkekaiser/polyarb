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
  `feeType` (e.g. `crypto_fees_v2`, or `null` for fee-free), and `feeSchedule.rate` (the
  taker rate). Fee-free markets have `feesEnabled:false`, `feeType:null`, `feeSchedule:null`.
  Computed fees are rounded to 5 dp; <0.00001 USDC rounds to zero.
- Implication for `MIN_PROFIT_BPS`: threshold should be lower for fee-free categories,
  higher for fee'd ones — derive per market from live params, per SPEC.

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

## WebSocket market channel

- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`.
- Subscribe: `{"assets_ids": ["<token_id>", ...], "type": "market"}`. Optional:
  `initial_dump` (bool, default true — snapshot on subscribe), `level` (1–3, default 2),
  `custom_feature_enabled` (default false).
- Dynamic (re)subscription without reconnect:
  `{"operation": "subscribe"|"unsubscribe", "assets_ids": ["<token_id>"]}`.

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
