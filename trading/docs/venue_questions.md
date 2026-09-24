# Questions to send the venues and the data provider

Each answer removes an assumption the engine currently makes. Most can be answered in one line.
The **why it matters** column is for you, so you can skip anything a support agent can't answer.

## Novig (API / partnerships team)

Suggested opener: *"We're building an automated trading integration against the Novig API for
pregame straights (NFL, NBA, MLB, NHL, WNBA) and have a few technical questions before we go live."*

| # | Question | Why it matters |
|---|----------|----------------|
| 1 | When a **buy limit order priced above the best offer** arrives, is it filled at each resting order's own price (price improvement) or at our limit price? | Decides `NOVIG_MULTI_LEVEL_MODE`. We default to one order per price level on the assumption that fills happen at our limit. |
| 2 | Is there an **immediate-or-cancel / fill-or-kill** time-in-force for orders? | Our unfilled remainders currently rest for up to 2s before we cancel them. IOC would remove that exposure. |
| 3 | What are the **API rate limits** (orders per second, requests per minute) and are they per account or per key? | Staggered orders send several orders at once, and the maker loop cancels in bulk. |
| 4 | Exact **order request body** for `POST /v1/orders` and bulk `DELETE /v1/orders` (field names, price units, client order id). | `novig_rest.ORDER_BODY_KEYS` is assumed. The first canary order proves or disproves it. |
| 5 | Shape of messages on the **`orders` WebSocket channel** (fill slips): field names, and is `filled_volume` cumulative per order? | The engine treats it as cumulative (`NOVIG_FILL_VOLUME_MODE`). |
| 6 | **Positions endpoint**: path, status values (open / settled), result and P&L fields, pagination. | Startup sync and settlement are built on assumed names (`settlement.py`). |
| 7 | Which field on an event carries the **scheduled start time**, and what happens to pregame orders and markets at kickoff (cancelled? converted to live?). | In live mode we never trade a game without a start time. If the field name differs, live mode trades nothing. |
| 8 | Do events move from `OPEN_PREGAME` to another status at start, and is there a **status push** on the WebSocket? | A faster live-game stop than our 30s list refresh near start. |
| 9 | Is there a **market-maker / liquidity-provider program** (rebates, fee credits, higher limits)? | Could turn the maker strategy from marginal to clearly profitable. |
| 10 | Confirm **no fees on pregame straights** for both makers and takers via the API, and how **Early Payout** works for a locked pair. | Fee assumptions and capital recycling. |
| 11 | **NFL tie** settlement on moneylines (dead heat at 50c, push/refund, or other). | Arbitrage scenarios assume 50c per leg on a tie. |
| 12 | Is there a **sandbox/QA** environment with the same API (we have `api-qa.novig.us`), and can our account get test funds there? | A zero-risk end-to-end test before the canary. |

## Kalshi (support / institutional desk)

| # | Question | Why it matters |
|---|----------|----------------|
| 1 | Game-winner **series tickers** for MLB, NHL and WNBA (we assume `KXMLBGAME`, `KXNHLGAME`, `KXWNBAGAME`). Are spreads and totals listed as markets too? | Coverage beyond NBA and NFL moneylines. |
| 2 | Current **taker and maker fee schedule** for sports markets (we use 7% x P x (1-P), rounded up per order; maker 1.75% on some markets). | Every cross-venue calculation. |
| 3 | Is there a **market-maker program** or **liquidity incentive** for sports markets? | The hedged-maker strategy would rest orders on Kalshi. |
| 4 | Recommended **time-in-force** for immediate execution (IOC / FOK) and rate limits for order create/cancel. | Kalshi order placement is the next build. |
| 5 | **NFL tie** settlement on game-winner markets (we assume 50c per side). | Tie scenarios. |
| 6 | Any restriction on **automated trading** or on holding offsetting positions across venues. | Compliance. |

## TheRundown (sales)

| # | Question | Why it matters |
|---|----------|----------------|
| 1 | Confirm the **Ultra plan price** and that it includes the real-time `/api/v2/ws/markets` WebSocket (a public profile lists Ultra at $399/month). | Our biggest fixed cost. |
| 2 | Is **Pinnacle (affiliate 3)** included, and is **Circa** or another sharp book available? | Fair value quality; blending several books. |
| 3 | Typical **latency** from a Pinnacle price change to the WebSocket frame. | Speed on stale-price edges. |
| 4 | Exact **WebSocket price-row fields** (we assume the same fields as `/markets/delta` rows, including `is_main_line`). | `therundown_feed.py` parsing. |
| 5 | How many **data points** does a 5-minute events snapshot for 5 leagues (main lines only) consume? | Staying inside 100M/month. |
| 6 | Is there a **free trial** of Ultra? | Measure edge before paying. |

## Compliance (do this once, before real money)

- Confirm each venue's terms allow **automated/API trading** from New York for your account type.
- Ask your accountant how **event-contract gains** are taxed and whether Kalshi and Novig issue 1099s.
- Keep `live_ledger.jsonl` and the venues' order histories. They're the audit trail.
