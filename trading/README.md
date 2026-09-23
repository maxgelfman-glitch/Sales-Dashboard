# Novig + Kalshi Trading Engine (paper trading)

Async dual-venue engine. It streams Novig and Kalshi order books, compares prices
to de-vigged sharp odds, and takes +EV positions (1/4 Kelly, $1,000 cap, Kalshi
edges net of taker fee). It also runs a two-sided maker loop on Novig and enforces
a cross-venue one-position-per-game lock with a scenario-checked arbitrage
exception and a $15,000 global exposure kill-switch.
**Paper trading only: `TRADING_MODE=live` is refused until fill confirmations are wired in.**

| File | Role |
|---|---|
| `ws_base.py` | Shared self-healing WebSocket loop (reconnect < 3s, ping/pong, watchdog) |
| `novig_feed.py` | Novig tape: `{"event":"subscribe","data":"tape"}`, outcomeId ticks, order books, registry |
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

## Still to confirm before real money
* Novig: events endpoint URL, JSON key spellings, tick envelope/actions, order body keys
  (all isolated in `novig_feed.py` / `novig_rest.py` headers).
* Novig private order/fill channel (required before `TRADING_MODE=live` can be enabled).
* OpticOdds / OddsJam field names in the example configs.
* Kalshi NFL tie settlement (cross-venue NFL moneyline hedges are refused until known).
