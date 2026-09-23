# Novig Trading Engine (paper trading)

Async engine that streams Novig order-book ticks, compares the best ask to
de-vigged sharp-book odds, sizes +EV positions with 1/4 Kelly (hard-capped at
$1,000), enforces a one-position-per-market lock and a $15,000 global exposure
kill-switch, and logs everything. **It places paper orders only — there is no live order code.**

| File | Role |
|---|---|
| `novig_feed.py` | Self-healing WebSocket feed: ticks (`market_id`, `price_cents`, `side`, `volume`) → order books → best ask |
| `sharp_feed.py` | Sharp-odds provider polling via a JSON config; strict 30s freshness |
| `execution.py` | De-vig, edge (>2.5%), 1/4 Kelly, $1,000 ceiling, `ExposureMonitor` kill-switch ($15,000) |
| `team_normalizer.py` | Maps team names across feeds ("NY Knicks" → "New York Knicks"); returns None rather than guess |
| `main_supervisor.py` | Orchestrator: position lock + arbitrage exception, liquidity sizing, paper orders, daily-rolling log |
| `mock_novig_server.py` | Local fake exchange for tests and simulation |

## Quick start
```bash
cd trading
pip install -r requirements.txt
python -m pytest -v                          # full self-test suite
python main_supervisor.py --simulate 30      # offline simulation, see logs/trading_engine.log
```

## Live (paper) mode — refuses to start on mock data
```bash
export NOVIG_BEARER_TOKEN=...                        # never commit secrets
export SHARP_API_KEY=...
export NOVIG_MARKETS_FILE=config/markets.json        # market_id -> league/teams/outcome/line
cp config/sharp_provider.example.json config/sharp_provider.json   # then map your provider's fields
export SHARP_PROVIDER_CONFIG=config/sharp_provider.json
python main_supervisor.py                            # QA feed by default; NOVIG_WS_URL=wss://api.novig.com/tape for prod
```

## Still to confirm before real money
* Novig tick field names/semantics (`novig_feed.py` header lists every assumption) — the
  docs at docs.novig.com could not be reached from the build sandbox.
* Where market metadata (`NOVIG_MARKETS_FILE`) comes from — likely Novig's REST markets endpoint.
* Whether Novig requires a subscription message after connecting (`subscribe_messages`).
* NFL moneyline tie rules (affects whether a moneyline hedge is truly risk-free).
