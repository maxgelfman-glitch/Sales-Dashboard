# Novig Trading Engine (paper trading)

Async engine that streams Novig prices, compares them to de-vigged sharp-book
odds, sizes +EV positions with 1/4 Kelly (hard-capped at $1,000), and logs
everything. **It places paper orders only — there is no live order code.**

| File | Role |
|---|---|
| `novig_feed.py` | Self-healing WebSocket feed (auth, ping/pong, stale-stream watchdog, <3s reconnect) |
| `execution.py` | Multiplicative de-vig, edge (>2.5%), 1/4 Kelly, $1,000 ceiling |
| `team_normalizer.py` | Maps team names across feeds ("NY Knicks" → "New York Knicks"); returns None rather than guess |
| `main_supervisor.py` | Runs feed + sharp poller + heartbeat, restarts crashed tasks, writes `logs/trading_engine.log` |
| `mock_novig_server.py` | Local fake exchange for tests and simulation |

## Quick start
```bash
cd trading
pip install -r requirements.txt
python -m pytest -v                          # full self-test suite
python main_supervisor.py --simulate 30      # offline simulation, see logs/trading_engine.log
export NOVIG_BEARER_TOKEN=...                # never commit this
python main_supervisor.py                    # Novig STAGING feed (paper only)
```

## Before going beyond simulation
* The Novig tape message format in `novig_feed.py` (`NovigMarketUpdate`) is **assumed**;
  confirm it against Novig's API docs.
* A real sharp-odds source must be wired in (`SHARP_API_URL` → `HttpSharpSource`,
  adapt the JSON mapping to the vendor).
