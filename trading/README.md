# Novig + Kalshi Trading Engine (paper trading)

Async dual-venue engine. It streams Novig and Kalshi order books, compares prices
to de-vigged sharp odds, and takes +EV positions (1/4 Kelly, $1,000 cap, Kalshi
edges net of taker fee). It also runs a two-sided maker loop on Novig and enforces
a cross-venue one-position-per-game lock with a scenario-checked arbitrage
exception and a $15,000 global exposure kill-switch.
**Paper trading by default.** Live Novig execution exists behind an explicit gate (below). Live Kalshi execution
exists behind a second gate (`KALSHI_LIVE_TRADING=1`); without it Kalshi is data-only in live mode.
Runs 24/7 on a small server: see `docs/deploy.md`. Open questions for the venues: `docs/venue_questions.md`.

| File | Role |
|---|---|
| `ws_base.py` | Shared self-healing WebSocket loop (reconnect < 3s, ping/pong, watchdog) |
| `novig_feed.py` | Novig tape: `{"event":"subscribe","data":"tape"}`, outcomeId ticks, order books, registry |
| `novig_private.py` | Execution-slip parsing (`order_id`, `status`, cumulative `filled_volume`, `price_cents`) |
| `novig_rest.py` | Startup bootstrap (events → markets → 2 outcomes) + order gateway (`POST`/`DELETE /v1/orders`) |
| `kalshi_feed.py` | Kalshi orderbook_delta feed, RSA-PSS auth, cents→probability→American translator, bootstrap |
| `sharp_feed.py` | Sharp provider polling (flat or OpticOdds/OddsJam per-outcome arrays), strict 30s freshness, multi-book consensus, in-play flag |
| `therundown_feed.py` | TheRundown v2 sharp source: REST snapshot + real-time WebSocket (`SHARP_PROVIDER=therundown`) |
| `devig.py` | Margin removal: multiplicative, power, Shin (`DEVIG_METHOD`) |
| `alerts.py` | CRITICAL events → phone/chat webhook (`ALERT_WEBHOOK_URL`) |
| `execution.py` | De-vig, edge, Kelly, caps, Kalshi fee, `ExposureMonitor`, `MakerEngine` (quotes + <200ms bulk cancel) |
| `team_normalizer.py` | Cross-feed team-name mapping (NFL, NBA, MLB, NHL, WNBA) |
| `main_supervisor.py` | Orchestrator: bootstrap, lock, arbitrage scenarios (incl. NFL ties), maker wiring, logging |
| `settlement.py` | Exchange positions: parsing, settlement P&L (WIN/LOSS/TIE 50¢/VOID), positions REST client |
| `research.py` | Measurement rows (`research/research-YYYYMMDD.jsonl`): decisions, entries, closing lines, markouts, depth, cross-venue gaps |
| `research_report.py` | Summarises the research rows: CLV, markouts, liquidity by time to start, gap frequency/profit, near misses |
| `dashboard.py` | Read-only Streamlit cockpit (separate process; tails `live_ledger.jsonl` + `trading_engine.log`) |
| `mock_novig_server.py` | Local fake exchange used by tests and `--simulate` |
| `docs/venue_questions.md` | Questions to send Novig, Kalshi and TheRundown (each answer removes an assumption) |
| `docs/deploy.md` | Running 24/7 on a small cloud server (systemd, secrets, alerts, backups) |

## Quick start
```bash
cd trading
pip install -r requirements.txt
python -m pytest -v                          # full self-test suite
python main_supervisor.py --simulate 30      # offline dual-venue simulation -> logs/trading_engine.log
```

## Paper mode against real feeds
All settings can live in one file: `python main_supervisor.py --env-file live.env` (template:
`config/live.env.example`; the real environment wins over the file).

**Measurement mode (no paid data):** leave `SHARP_PROVIDER` and `SHARP_PROVIDER_CONFIG` unset. The engine
records Novig/Kalshi prices, cross-venue locked-profit gaps, near misses and liquidity by time to start.
Nothing that needs a fair value trades. This alone answers whether the arbitrage/hedged-maker strategies
have enough room.

**TheRundown (recommended fair-value source):** `SHARP_PROVIDER=therundown`, `THERUNDOWN_API_KEY=...`,
`THERUNDOWN_AFFILIATE_IDS=3` (Pinnacle). The Ultra plan streams price changes over a WebSocket; without
it set `THERUNDOWN_WEBSOCKET=0` (REST snapshots every 15s).

Generic JSON-mapped provider (OpticOdds/OddsJam):
```bash
export NOVIG_BEARER_TOKEN=...  SHARP_API_KEY=...
export NOVIG_EVENTS_URL=https://...           # Novig events endpoint (confirm path in Novig docs)
cp config/sharp_provider.opticodds.example.json config/sharp_provider.json   # or the oddsjam example
export SHARP_PROVIDER_CONFIG=config/sharp_provider.json
# optional Kalshi:
export KALSHI_ENABLED=1 KALSHI_ENV=demo KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=/secure/kalshi.pem
python main_supervisor.py
```

## Leagues, venues and fair value
* Leagues: NFL, NBA, MLB, NHL, WNBA, **college football (NCAAF), college basketball (NCAAB)** and **tennis
  (ATP + WTA)**. Moneyline, spread (incl. run/puck line) and total.
  * College teams and tennis players have no fixed table. Every listed game is registered (Novig first) and
    other venues' spellings are matched game by game, conservatively: "Georgia" never matches "Georgia
    State/Tech"; "Miami (FL)" never matches "Miami (OH)"; "C. Alcaraz" = "Carlos Alcaraz". Ambiguous = not
    traded. One home/away orientation per game across venues.
  * Tennis: no cross-venue locked pairs or hedges (retirements settle differently by venue); directional only.
  * Soccer is excluded (3-way moneyline with a draw: needs a 3-outcome model).
* Venues from New York: **Novig** and **Kalshi** (live, gated). **ProphetX** as a price feed for paper trading
  and research (`PROPHETX_ENABLED=1`, partner API keys). Its 2%-of-winnings fee is in every edge and pair
  calculation. Its field names are assumed from public docs mirrored by an open-source client: run
  `python prophetx_feed.py --probe` with your keys and share the saved file so they can be checked before any
  live ProphetX orders are added. Polymarket US (New York sued it Sept 24, 2026) and Sporttrade (not in NY) are
  not integrated.
* `DEVIG_METHOD` = multiplicative (default) | power | shin. Power/Shin take more margin off longshots.
  Every DECISION research row records all three so the report can show which one holds up at the close.
* `SHARP_BOOK_WEIGHTS=pinnacle:2,circa sports:1`: fresh books quoting the same number are blended into one
  consensus fair value; unlisted books are ignored (`*:1` includes them).
* A sharp move re-checks every venue price of that market immediately: the stale-price edge no longer waits
  for the venue's next tick.
* Optional taker filters, off by default: `TAKER_REQUIRE_SHARP_MOVED_LAST=1` and
  `TAKER_MAX_SHARP_MOVE_AGE_SECONDS=5`. Turn them on only if the report's WHO MOVED FIRST section shows
  "sharp, < 5s" beating "venue".

## Locked pairs (the core arbitrage, no fair value needed)
When the two sides of a market, on any venues, cost less than the payout after fees in every outcome
(NFL ties included, whole-number lines excluded), the engine buys **both** at once (`ARB_PAIRS_ENABLED=1`,
default). This needs no sharp line, so it also runs in measurement mode. Neither side has to beat the
sharp line on its own. Size = the thinner side's best level, capped at $1,000 per leg, the live canary
stake, and a per-game cap applied to the worst case (one leg fills, the other doesn't). In live mode both
venues must be live-enabled. If one leg misses, the other stays as a normal position and the regular hedge
path keeps trying to complete it.

## Risk controls
* `GAME_EXPOSURE_LIMIT_USD` (default $1,000): unhedged money per game across its moneyline, spread and total
  (they are correlated). Hedges are always allowed and free room. Maker quotes only on games we hold nothing in.
* `DAILY_LOSS_LIMIT_USD` (default $2,000): settled loss per UTC day that stops new positions and quotes until
  00:00 UTC. Survives restarts.
* `ALERT_WEBHOOK_URL`: every CRITICAL event goes to Slack/Discord/ntfy (see `docs/deploy.md`).
* Plus: $1,000 per position, $15,000 total exposure, live canary $10/$100, pregame cutoffs, live-game stop.

## Pregame cutoffs and the live-game stop (never trade into a live game)
Every game gets its scheduled start time from the Novig events data (Kalshi: `occurrence_datetime`).
* **Maker cutoff** `MAKER_CUTOFF_MINUTES` (default 3): every resting quote on the game is pulled. Quotes go
  first because late news (lineups, injuries) picks off a stale resting quote.
* **Taker cutoff** `TAKER_CUTOFF_MINUTES` (default 1): no new orders on the game, hedges included. Taker
  orders fill or are cancelled within 2s, so nothing is left resting when the game starts.
* Per league: `CUTOFF_OVERRIDES=NBA=1/3,NFL=1/3` (taker/maker minutes).
* **Live-game stop**, whatever the clock says: the sharp feed marks the game in play (`is_live` / status field;
  that line is never used as a fair price), or the game drops out of Novig's pregame list within an hour of its
  start (the list is re-read every 30s while a game is within 15 minutes of starting).
* **In live mode a game with no known start time is never traded.** The start-time field name is assumed
  (`novig_rest.START_TIME_KEYS`); if Novig uses another name, live mode trades nothing, which is the safe failure.
* Paper mode keeps evaluating between the taker cutoff and the start and records what it *would* have done
  (`blocked` decisions), so the report shows whether a later cutoff would pay.

## Size: buying through the book, partial hedges
* Takers buy through several ask levels while **each** level still clears the 2.5% edge, capped by the
  1/4-Kelly stake of the worst level used, $1,000 per position and the live canary.
* `NOVIG_MULTI_LEVEL_MODE` decides how that goes to Novig:
  * `staggered` (default): one order per level, each priced at that level, all sent at once. Novig is
    believed to fill a taker at its limit price rather than at each resting price, so a single order at the
    worst level would pay the worst price for everything. With staggered orders no contract can cost more
    than its own level. If a level vanishes first, its tranche rests at its (still profitable) price until
    the 2s fill timeout cancels it. All tranches feed one position leg; ledger rows `ORDER` (one per
    tranche), `TRANCHE_DONE` and one `DONE`.
  * `single`: one order limited at the worst level, sized so that even a fill entirely at that limit fits
    every cap. Only right if the exchange fills cheaper levels first; a fill entirely at the limit logs
    `PRICE_IMPROVEMENT_MISSING` and falls back to the best level for the session.
  * `off`: best level only.
* Hedges do the same while every level still locks at least 1c per contract in every outcome. A hedge
  may be partial (at least 10 contracts); the rest of the first leg stays a directional position and can be
  hedged later.

## Measuring the edge (research data)
On by default (`RESEARCH_ENABLED=1`, folder `RESEARCH_DIR=research`), in paper and live mode:
```bash
python main_supervisor.py --simulate 60      # demo data -> logs/research/
python research_report.py --dir logs/research
python research_report.py --since 2026-10-01 # real runs: ./research
```
The report answers: did our entries beat the closing line (CLV), how that CLV changes with time to start
(including trades the cutoff blocked), are maker fills picked off (markouts, also by time to start),
how much size sits at the best price by time to start, and how often, how long and how deep cross-venue
locked-profit gaps are (Novig free, Kalshi taker fee, ProphetX 2% of winnings, NFL ties at 50c).
Two to four weeks of paper data are enough to decide whether the strategy is worth scaling.

## Dashboard (read-only cockpit)
```bash
pip install -r requirements-dashboard.txt
nice -n 10 streamlit run dashboard.py        # http://127.0.0.1:8501 (local only)
```
Runs as its own process, imports no engine code, opens no exchange connections and only reads the ledger
and engine log (new bytes only). Capital and the profit curve come from the engine's `SETTLE` rows (`net_profit_usd`, `resulting_capital_pool`).

## Live mode (real orders on Novig)
Template: `config/live.env.example`. Always run the offline report first:
```bash
python main_supervisor.py --check-config      # plain-text report; opens no connections, writes no ledger
```
Minimum for `TRADING_MODE=live`: `LIVE_TRADING_ACKNOWLEDGED=yes`, `NOVIG_BEARER_TOKEN`, a valid
`NOVIG_WS_URL` (default `wss://api.novig.com/tape`). Prices and our executions share that ONE socket:
the engine sends `{"event":"subscribe","channel":"tape"}` and `{"event":"subscribe","channel":"orders"}`.
`filled_volume` is treated as a cumulative total per order id (`NOVIG_FILL_VOLUME_MODE=cumulative`).

**Canary lock:** live always starts at **$10 max stake, $100 exposure, maker off**. Raising any of these
(up to the hard $1,000 / $15,000 ceilings) is refused unless `LIVE_SCALE_APPROVED_BY` is set; the
approval is logged CRITICAL and written to `live_ledger.jsonl` at launch.

**Ledger** (`$TRADING_LOG_DIR/live_ledger.jsonl`, append-only JSON lines): `CANARY_LIMITS` /
`SCALE_UP_AUTHORIZED`, `SESSION_START`, `ORDER` (exact POST body), `REJECTED`, `SLIP` (raw execution
slip), `FILL` (delta + running total + cost), `CANCEL` (taker timeout / maker, with reason),
`CANCEL_FAILED`, `DONE`, `UNCONFIRMED`.

Safety behaviour: stake reserved + game locked before sending; remainder cancelled after 2s; until the
first execution slip proves the orders channel, a no-fill order KEEPS its lock and reservation
(`LIVE_UNCONFIRMED`, CRITICAL); socket down ⇒ quotes bulk-cancelled, no new orders.

**Settlement & restart safety (live):** before any order, the engine loads every OPEN Novig position
(`RESTORE` rows) and refuses to trade if that fails. Every 15 minutes (`SETTLEMENT_SWEEP_SECONDS`) it fetches
SETTLED positions, releases their exposure and writes one `SETTLE` row each (idempotent across sweeps and
restarts). The same sweep resolves `UNCONFIRMED` orders against the exchange and restores untracked
positions (e.g. manual trades). Endpoint settings: `NOVIG_POSITIONS_PATH` (default `/v1/positions`),
`NOVIG_POSITIONS_STATUS_PARAM` (`status`), `NOVIG_OPEN_STATUS` (`OPEN`), `NOVIG_SETTLED_STATUS` (`SETTLED`).

### Rollout
1. Paper mode against real feeds; compare decisions with the Novig UI.
2. Canary (default live limits). Reconcile every ledger line against Novig's order history.
3. Only then set `LIVE_SCALE_APPROVED_BY` and raise limits / enable `MAKER_MODE` step by step.

Regenerate config schemas with `python config/generate_schemas.py` (a test fails if they drift).

## Live Kalshi execution (`KALSHI_LIVE_TRADING=1`, second gate)
Needs `TRADING_MODE=live`, `KALSHI_ENABLED=1`, `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` (`KALSHI_ENV=demo` first).
* Orders: `POST /portfolio/orders`, buy YES on the outcome's ticker, **immediate-or-cancel**, so nothing rests.
  Client ids carry a per-session tag, so they never repeat across restarts.
* Fills: the authenticated `fill` channel on the same Kalshi socket (incremental counts). The taker fee is
  added to each fill's cost, rounded up. If no fill arrives, Kalshi's order record (`GET /portfolio/orders/{id}`)
  confirms a zero fill before the reservation is released, and books any fill the socket missed.
* Positions: `/portfolio/positions` at startup and `/portfolio/settlements` in the 15-minute sweep (payout =
  revenue), so Kalshi exposure is released like Novig's.
* Same canary stake/exposure limits, per-game cap, cutoffs and live-game stop as Novig. Cross-venue hedges
  (Novig ↔ Kalshi) are allowed in live mode once this gate is on.

## Still to confirm before real money
* Novig `orders` channel slip shape and order body keys (first canary order proves or disproves them).
* Novig matching: whether a buy limit above the best ask fills cheaper levels at their own prices. The
  default `staggered` mode does not depend on it.
* Novig positions endpoint: path, status filter values, field names, pagination (`settlement.py`).
* Novig REST host `api.novig.us` for `/v1/orders`; whether events embed markets; pagination beyond
  `limit=100` (the bootstrap logs a warning for both).
* OpticOdds record paths in `config/sharp_provider.opticodds.example.json`.
* Kalshi order routing + fills (not built: Kalshi is data-only in live mode).
