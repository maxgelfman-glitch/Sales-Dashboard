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

## Novig (v3 API)
Novig's real API (self-serve beta, docs.novig.com) is in `novig_v3.py`, written against its published docs
and OpenAPI document, with the signer checked against Novig's 30 official test signatures.
* **Keys:** a management key from the Novig app opens a subaccount for the engine;
  `python novig_v3.py setup` does it and writes the subaccount's trading key (Ed25519) to `keys/`. Put
  `NOVIG_KEY_ID` and `NOVIG_PRIVATE_KEY_PATH` in `live.env` (template in `config/live.env.example`). Then
  `python novig_v3.py echo` proves the key, clock and signature. `python novig_v3.py probe` prints leagues,
  sample events, outcome names and a book, and what the engine would track (no secrets in it).
* **Location:** orders need the Novig app to have geolocated you in a legal state within 3 days (HTTP
  451 otherwise: open the app on your phone). No VPN. A normal cloud server is fine.
* **Units:** one Novig contract pays 1c; the engine's pays $1. `novig_v3.py` converts (x100) everywhere.
* **Book:** every Novig order is a buy. A bid at 0.665 on one team is the other team's ask at 0.335, so
  each outcome's asks are built from the other outcome's bids. Our own resting orders are left out.
* **Fills** happen at the resting order's price, which can be better than our limit (Novig's docs). The
  paper simulator now fills Novig the same way.
* **Fees:** game markets charge takers only while the game is live. Pregame is free; the engine never
  trades live, and Novig voids every resting order at go-live anyway.
* **Socket:** one signed socket carries the `book` of every tracked market (up to 2,048) and our private
  `orders`. Takers are IOC; maker quotes are GTT (they expire after `NOVIG_MAKER_TTL_SECONDS`). Live start
  cancels anything left resting in the engine's subaccount. GOLIVE / START / CLOSE near the start mark
  the game live.

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
export NOVIG_KEY_ID=...  NOVIG_PRIVATE_KEY_PATH=keys/novig-trading-engine.pem  SHARP_API_KEY=...
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
  * `staggered` (default): one order per level, each priced at that level, all sent at once. No contract
    can cost more than its own level. (Novig's v3 docs say a fill happens at the resting price, so `single`
    would also be safe there; staggered stays the default because it does not depend on it.) If a level vanishes first, its tranche rests at its (still profitable) price until
    the 2s fill timeout cancels it. All tranches feed one position leg; ledger rows `ORDER` (one per
    tranche), `TRANCHE_DONE` and one `DONE`.
  * `single`: one order limited at the worst level, sized so that even a fill entirely at that limit fits
    every cap. Only right if the exchange fills cheaper levels first; a fill entirely at the limit logs
    `PRICE_IMPROVEMENT_MISSING` and falls back to the best level for the session.
  * `off`: best level only.
* Hedges do the same while every level still locks at least 1c per contract in every outcome. A hedge
  may be partial (at least 10 contracts); the rest of the first leg stays a directional position and can be
  hedged later.

## Combo (parlay) quoting on Kalshi (`COMBO_QUOTER`)
Retail loses heavily on Kalshi combos (reported ~$294M net in 2026, implied margin ~14.7%), and only a handful of bots
quote them. Kalshi prices combos by **Request For Quote**: a user builds a combo, makers quote, the user accepts, and
**the maker confirms (last look)** before anything executes. `combo_quoter.py` quotes the SHORT side of combos:
* **What it quotes:** every league we have sharp prices for (NFL, NBA, MLB, NHL, WNBA, college, tennis). Shadow
  prices parlays of up to 6 legs, same-game ones included; real quotes default to parlays of up to 4 legs on
  **different games** (`COMBO_LIVE_KINDS=xgame`, `COMBO_LIVE_MAX_LEGS=4`, optional `COMBO_LIVE_LEAGUES=NFL,NBA`).
  Same-game legs (same event, or the game code inside Kalshi tickers) move together, so they stay shadow-only
  until their own numbers hold up (`COMBO_LIVE_KINDS=xgame,sgp`). Every leg needs a fresh fair value: the sharp
  line, or Kalshi's own book when its bid/ask spread is ≤3¢ (with an extra margin).
* **Pricing: the less sure, the wider and the smaller.** Fair = product of the legs' probabilities. Margin =
  4% + 2% per leg + an uncertainty margin per leg: league (NFL 0, NBA/MLB/NHL +0.5%, WNBA/college/tennis +1%,
  other +2%), line age (up to +1%), sharp books disagreeing (spread ÷ probability; over 3¢ apart = no quote),
  one book only (+0.5%, no second opinion: add a second sharp book to `THERUNDOWN_AFFILIATE_IDS` and weight
  Pinnacle higher in `SHARP_BOOK_WEIGHTS`), Kalshi-book pricing (+1%), same-game (+8% per extra leg) and a
  requester with a winning record (+4%; declined above +15% ROI over 15+ parlays: we cannot limit winners the
  way sportsbooks do). Size shrinks with the same uncertainty, down to 25% of the per-combo cap.
* **Fee:** since 2026-08-20 Kalshi charges combo makers half the taker fee, 0.035·P·(1−P), except parlays of
  NFL legs on different games. It is added on top of the margin. Expected return on collateral must be ≥1%,
  which skips the 0.1¢ longshot trap.
* **Sportsbook check (optional feed):** when we are unsure, never quote tighter than a retail sportsbook's
  price for the same parlay (minus 3%); if our fair is above the sportsbook's price, we decline: the model is
  the likely error. Until a feed exists, spot-check same-game parlays by hand in a sportsbook app.
* **Risk book:** caps on max loss per combo, per leg (popular legs appear in many combos) and in total, checked
  when quoting and again at confirmation. Quotes expire after 10s. The last look re-prices every leg and declines
  if the combo moved against us. The daily loss stop and exposure kill-switch block new quotes.
* **Modes:**
  * `shadow` (start here): prices every open RFQ and sends nothing. It then checks the price each combo actually
    traded at (would ours have won?) and its result (would it have paid?). Report section [COMBO QUOTING],
    broken down by slice (leagues : legs : x/sgp), with a warning when we would have been 10%+ cheaper than
    where the parlay traded (usually our model, not a gift).
  * `demo`: real quotes on Kalshi's demo exchange (`KALSHI_ENV=demo`).
  * `live`: needs `TRADING_MODE=live`. Default caps are $25 per combo, $150 per leg and $1,000 total.
* **Gates before real money:** (0) confirm with Kalshi that a regular account can quote combos in production;
  (1) shadow, per slice: would-win rate ≥5% at a median margin ≥8%; (2) leg fair values track closing prices; (3) tiny live
  results within ~30% of shadow expectations; (4) scale up step by step, **after a CPA opinion** (if treated as
  gambling, only 90% of losses are deductible from 2026, which hits high-turnover short-combo books hard).
* **Assumed, to verify on demo:** the requester buying YES pays 1 − our `no_bid`; quotes cover the full RFQ size;
  the combo maker fee and its NFL exemption (from news reports); that makers can list other members' open RFQs
  and see their `creator_id`.

## Novig parlays (`NOVIG_RFQ`) and Novig's public data
**Is it worth it? Measure first:** `python novig_data.py --days 7` downloads Novig's free daily trade files
(data.novig.com) on your machine and prints a short summary to paste back: parlays per day, retail stake,
typical price and legs, how many quoters split each trade, busiest hours, and straight volume by league and
market type. No installs needed (standard library).

**Quoting:** `novig_rfq.py` answers Novig's parlay auctions as a registered pricer (liquidity provider), over
Novig's RFQ websocket. The auction lasts 3 seconds, the lowest price wins, and the winner has 1 second to confirm.
Quoters pay no fee. Pricing is the Kalshi quoter's (same per-leg uncertainty margins, same-game parlays
shadow-only, size shrinking with doubt) with its own risk book.
* **Access:** email developers@novig.com to become an LP (W-9; QA test access within ~2 business days;
  production generally needs a $30,000 deposit). Novig then gives you API credentials: put
  `NOVIG_RFQ_ACCESS_TOKEN` (or `NOVIG_RFQ_CLIENT_ID`, `NOVIG_RFQ_CLIENT_SECRET`, `NOVIG_RFQ_TOKEN_URL`) in
  `live.env`, then register once: `python novig_rfq.py register` (`status` shows the registration and your
  open collateral).
* **Modes:** `shadow` (prices every auction, sends nothing; Novig reports every auction's winning price, so it
  learns where we would have won), `qa` (real quotes on Novig's test exchange), `live` (`TRADING_MODE=live`).
* **Caps:** `NOVIG_RFQ_MAX_LOSS_PER_COMBO`, `NOVIG_RFQ_MAX_LEG_EXPOSURE`, `NOVIG_RFQ_MAX_TOTAL_LIABILITY`
  (default to the `COMBO_*` values). Our max stake per quote is set so the collateral fits the caps; below the
  round's minimum stake we decline.
* **Results** appear in the report's [COMBO QUOTING] table under `novig <slice>`.
* **To confirm on QA:** that `result` on `GET /rfq/executions` is the parlay's result from the bettor's side;
  the token URL Novig gives you.

## Realistic paper execution and the size ladder
Paper trading defaults to `PAPER_EXECUTION=simulated`. Every paper order goes through the **same order path
as live trading** (reservations, tranches, fees, positions) against a simulated exchange (`sim_exchange.py`):
* The book is only looked at `SIM_LATENCY_MS` (default 500) after the order is sent. Anything that moved or was
  taken in between is missed.
* Of the depth still there at our price or better, only `SIM_DEPTH_HAIRCUT` (default 50%) is ours. Depth we
  already took stays taken until the venue re-quotes.
* Kalshi fills at each level's price; Novig and ProphetX at the order's limit (conservative). Nothing rests.

`research_report.py` then shows, per venue and order size: fill rate, how often we got nothing, the price paid
versus the price seen, and speed ([EXECUTION]). It also shows how long +EV prices survived ([EDGE SURVIVAL]) and
the **expected profit of what actually filled, per month** ([PROJECTED MONTHLY]).
`PAPER_EXECUTION=instant` restores the old, optimistic behaviour.

What no simulation can tell you is how much you really get at size: other traders' orders racing ours, and
makers pulling quotes when hit. The live **size ladder** finds that out with little at risk:
1. Canary: `LIVE_MAX_STAKE_USD=10`. Confirms orders, fills, settlement and real latency (LIVE rows in [EXECUTION]).
2. Then $50, then $250, then $1,000 per order. Each step needs `LIVE_SCALE_APPROVED_BY` and at least ~50 live
   orders at the current size.
3. Move up only while the LIVE fill rate at the new size stays close to the simulated fill rate. If fills collapse
   as size grows (asking for $1,000 and getting $15), stop at the last size that held: that is the strategy's
   real capacity.


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
Minimum for `TRADING_MODE=live`: `LIVE_TRADING_ACKNOWLEDGED=yes` and a Novig v3 trading key
(`NOVIG_KEY_ID`, `NOVIG_PRIVATE_KEY_PATH`; see "Novig (v3 API)"). Prices and our executions share one
signed socket; each fill event carries its own quantity.

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
positions (e.g. manual trades). Novig v3: open positions from `GET /v3/account/positions`; a settled one
is read from its market's grade (WIN / LOSS / PUSH, or a fair-value price that pays that fraction).

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
* Novig v3 event descriptions ("Away at Home") and spread outcome names containing the team name: run
  `python novig_v3.py probe` once and check the parsed home/away. Everything else follows Novig's docs.
* The first canary order on Novig: reconcile it against the app.
* OpticOdds record paths in `config/sharp_provider.opticodds.example.json`.
* Kalshi order routing + fills (not built: Kalshi is data-only in live mode).
