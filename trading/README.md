# Novig + Kalshi Trading Engine (paper trading)

Async dual-venue engine. It streams Novig and Kalshi order books, compares prices
to de-vigged sharp odds, and takes +EV positions (1/4 Kelly, $1,000 cap, Kalshi
edges net of taker fee). It also runs a two-sided maker loop on Novig and enforces
a cross-venue one-position-per-game lock with a scenario-checked arbitrage
exception and a $15,000 global exposure kill-switch.
**Paper trading by default.** Live Novig execution exists behind an explicit gate (below); Kalshi is data-only in live mode.

| File | Role |
|---|---|
| `ws_base.py` | Shared self-healing WebSocket loop (reconnect < 3s, ping/pong, watchdog) |
| `novig_feed.py` | Novig tape: `{"event":"subscribe","data":"tape"}`, outcomeId ticks, order books, registry |
| `novig_private.py` | Execution-slip parsing (`order_id`, `status`, cumulative `filled_volume`, `price_cents`) |
| `novig_rest.py` | Startup bootstrap (events → markets → 2 outcomes) + order gateway (`POST`/`DELETE /v1/orders`) |
| `kalshi_feed.py` | Kalshi orderbook_delta feed, RSA-PSS auth, cents→probability→American translator, bootstrap |
| `sharp_feed.py` | Sharp provider polling (flat or OpticOdds/OddsJam per-outcome arrays), strict 30s freshness |
| `execution.py` | De-vig, edge, Kelly, caps, Kalshi fee, `ExposureMonitor`, `MakerEngine` (quotes + <200ms bulk cancel) |
| `team_normalizer.py` | Cross-feed team-name mapping |
| `main_supervisor.py` | Orchestrator: bootstrap, lock, arbitrage scenarios (incl. NFL ties), maker wiring, logging |
| `dashboard.py` | Read-only Streamlit cockpit (separate process; tails `live_ledger.jsonl` + `trading_engine.log`) |
| `mock_novig_server.py` | Local fake exchange used by tests and `--simulate` |

## Quick start
```bash
cd trading
pip install -r requirements.txt
python -m pytest -v                          # full self-test suite
python main_supervisor.py --simulate 30      # offline dual-venue simulation -> logs/trading_engine.log
```

## Paper mode against real feeds
```bash
export NOVIG_BEARER_TOKEN=...  SHARP_API_KEY=...
export NOVIG_EVENTS_URL=https://...           # Novig events endpoint (confirm path in Novig docs)
cp config/sharp_provider.opticodds.example.json config/sharp_provider.json   # or the oddsjam example
export SHARP_PROVIDER_CONFIG=config/sharp_provider.json
# optional Kalshi:
export KALSHI_ENABLED=1 KALSHI_ENV=demo KALSHI_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=/secure/kalshi.pem
python main_supervisor.py
```

## Dashboard (read-only cockpit)
```bash
pip install -r requirements-dashboard.txt
nice -n 10 streamlit run dashboard.py        # http://127.0.0.1:8501 (local only)
```
Runs as its own process, imports no engine code, opens no exchange connections and only reads the ledger
and engine log (new bytes only). Settled P&L needs `SETTLE` ledger events, which the engine does not write yet.

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

### Rollout
1. Paper mode against real feeds; compare decisions with the Novig UI.
2. Canary (default live limits). Reconcile every ledger line against Novig's order history.
3. Only then set `LIVE_SCALE_APPROVED_BY` and raise limits / enable `MAKER_MODE` step by step.

Regenerate config schemas with `python config/generate_schemas.py` (a test fails if they drift).

## Still to confirm before real money
* Novig `orders` channel slip shape and order body keys (first canary order proves or disproves them).
* Novig REST host `api.novig.us` for `/v1/orders`; whether events embed markets; pagination beyond
  `limit=100` (the bootstrap logs a warning for both).
* OpticOdds record paths in `config/sharp_provider.opticodds.example.json`.
* Kalshi order routing + fills (not built: Kalshi is data-only in live mode).
