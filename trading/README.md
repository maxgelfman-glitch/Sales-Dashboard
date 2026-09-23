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
| `novig_private.py` | Private fill channel: execution slips (`order_id`, `status`, `filled_volume`, `price_cents`) |
| `novig_rest.py` | Startup bootstrap (events → markets → 2 outcomes) + order gateway (`POST`/`DELETE /v1/orders`) |
| `kalshi_feed.py` | Kalshi orderbook_delta feed, RSA-PSS auth, cents→probability→American translator, bootstrap |
| `sharp_feed.py` | Sharp provider polling (flat or OpticOdds/OddsJam per-outcome arrays), strict 30s freshness |
| `execution.py` | De-vig, edge, Kelly, caps, Kalshi fee, `ExposureMonitor`, `MakerEngine` (quotes + <200ms bulk cancel) |
| `team_normalizer.py` | Cross-feed team-name mapping |
| `main_supervisor.py` | Orchestrator: bootstrap, lock, arbitrage scenarios (incl. NFL ties), maker wiring, logging |
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

## Live mode (real orders on Novig)
Refused unless every item is present and valid:
```bash
export TRADING_MODE=live
export LIVE_TRADING_ACKNOWLEDGED=yes                  # explicit human sign-off
export NOVIG_BEARER_TOKEN=...
export NOVIG_PRIVATE_WS_URL=wss://...                 # private fill channel (validated; must be wss://host/...)
export NOVIG_FILL_VOLUME_MODE=cumulative              # or incremental — confirm with Novig
# optional
export NOVIG_PRIVATE_SUBSCRIBE='{"event":"subscribe","data":"orders"}'
export LIVE_MAX_STAKE_USD=50 LIVE_EXPOSURE_LIMIT_USD=500   # may only LOWER $1,000 / $15,000
```
Defaults in live mode: tape `wss://api.novig.com/tape`, bootstrap
`https://novig.com/nbx/v2/emm/events?status=OPEN_PREGAME&limit=100`, orders `https://novig.com/v1/orders`
(override with `NOVIG_WS_URL`, `NOVIG_EVENTS_URL`, `NOVIG_API_BASE`).

How live orders are accounted: stake is reserved and the game locked before sending; private fill
slips set the real position; any remainder is cancelled after 2s and the unused reservation released.
If the private channel or tape drops, all quotes are bulk-cancelled and no new orders are sent.

### Recommended rollout
1. Paper mode against real feeds for several sessions; compare logged decisions with the Novig UI.
2. Live canary: `LIVE_MAX_STAKE_USD=10`, `LIVE_EXPOSURE_LIMIT_USD=100`, `MAKER_ENABLED=0`; reconcile
   every `ORDER LIVE` / `FILL` log line against Novig's order history.
3. Enable the maker, then raise caps step by step.

## Still to confirm before real money
* Novig private channel URL + subscribe message, and whether `filled_volume` is cumulative or incremental.
* Novig host for REST (`novig.com` per brief vs `api.novig.us` in Novig's docs example), order body keys,
  whether events embed markets, and pagination beyond `limit=100` (the bootstrap logs a warning for both).
* OpticOdds record paths in `config/sharp_provider.opticodds.example.json`.
* Kalshi order routing + fills (not built: Kalshi is data-only in live mode).
