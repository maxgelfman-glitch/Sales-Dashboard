"""
Self-test: dashboard.py

    * isolation: the dashboard never imports engine modules
    * tailing: only new bytes are read; partial lines, rotation and truncation handled
    * CONTRACT: exposure / positions / volume rebuilt from a ledger written by the REAL engine
      match the engine's own numbers at every step
    * heartbeat, audit colouring, settled P&L curve
    * the Streamlit app itself renders without exceptions (AppTest, headless)
"""

import ast
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import dashboard as db

ENGINE_MODULES = {"main_supervisor", "execution", "novig_feed", "novig_rest", "novig_private", "kalshi_feed",
                  "sharp_feed", "ws_base", "team_normalizer", "mock_novig_server"}


# ================================================================ isolation
def test_dashboard_source_imports_no_engine_module():
    tree = ast.parse(Path("dashboard.py").read_text())
    imported = {n.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & ENGINE_MODULES


def test_importing_dashboard_loads_no_engine_or_network_module():
    code = ("import sys, dashboard; bad = {'main_supervisor','execution','novig_feed','websockets','aiohttp',"
            "'kalshi_feed','sharp_feed'} & set(sys.modules); print(sorted(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


# ================================================================ tailing
def test_tail_reads_only_new_complete_lines(tmp_path):
    f = tmp_path / "l.jsonl"
    f.write_text('{"ts": 1, "event": "A"}\n{"ts": 2, "ev')
    t = db.FileTail(f)
    assert t.read_new() == '{"ts": 1, "event": "A"}\n'           # half-written line held back
    with f.open("a") as fh:
        fh.write('ent": "B"}\n')
    assert t.read_new() == '{"ts": 2, "event": "B"}\n'
    assert t.read_new() == ""                                      # nothing new: nothing read
    assert t.offset == f.stat().st_size


def test_tail_follows_rotation_and_truncation(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("one\ntwo\n")
    t = db.FileTail(f)
    assert t.read_new() == "one\ntwo\n"
    f.rename(tmp_path / "x.log.1")                                 # midnight rotation
    f.write_text("three\n")
    assert t.read_new() == "three\n" and t.resets == 1
    f.write_text("")                                               # truncated in place
    f.write_text("four\n")
    assert t.read_new() == "four\n"


def test_tail_bootstrap_starts_at_a_line_boundary(tmp_path):
    f = tmp_path / "big.log"
    f.write_text("".join(f"line {i:05d}\n" for i in range(1000)))
    t = db.FileTail(f, bootstrap_bytes=100)
    lines = t.read_new().splitlines()
    assert lines and all(l.startswith("line ") and len(l) == 10 for l in lines) and lines[-1] == "line 00999"


def test_ledger_store_reads_incrementally_and_resets_on_replacement(tmp_path):
    f = tmp_path / "l.jsonl"
    f.write_text("".join(json.dumps({"ts": i, "event": "X"}) + "\n" for i in range(3)))
    store = db.LedgerStore(f)
    assert len(store.refresh()) == 3
    with f.open("a") as fh:
        fh.write(json.dumps({"ts": 9, "event": "Y"}) + "\n")
    assert list(store.refresh()["event"]) == ["X", "X", "X", "Y"]
    f.unlink()
    f.write_text(json.dumps({"ts": 1, "event": "NEW"}) + "\n")
    assert list(store.refresh()["event"]) == ["NEW"]              # no duplicated history


def test_parse_ledger_skips_corrupt_lines():
    text = '{"ts": 1, "event": "A"}\nnot json\n{"ts": 2, "event": "B"}\n{"no_event": true}\n'
    assert list(db.parse_ledger_chunk(text)["event"]) == ["A", "B"]
    assert db.parse_ledger_chunk("").empty


# ================================================================ CONTRACT with the real engine
class FakeGateway:
    live, api_base = True, "fake://novig"

    def __init__(self):
        self.n, self.cancels = 0, []

    async def place_limit(self, *a):
        self.n += 1
        return f"ex-{self.n}"

    async def cancel_orders(self, ids):
        self.cancels.append(ids)

    async def close(self):
        pass


def engine_and_ledger(tmp_path, confirmed=True, timeout=60.0):
    from execution import ExposureMonitor
    from main_supervisor import DEMO_MARKETS, DEMO_SHARP_LINES, LivePlan, Supervisor
    from novig_feed import MarketRegistry
    from sharp_feed import MockSharpSource
    ledger = tmp_path / "live_ledger.jsonl"
    sup = Supervisor(feed_url="ws://127.0.0.1:1/x", token="t", sharp_fetch=MockSharpSource(DEMO_SHARP_LINES),
                     registry=MarketRegistry(DEMO_MARKETS), live=True, order_gateway=FakeGateway(),
                     maker_enabled=False, max_stake=10.0, exposure=ExposureMonitor(100.0), ledger_path=ledger,
                     taker_fill_timeout=timeout,
                     live_plan=LivePlan(max_stake=10, exposure_limit=100, maker_enabled=False, scaled_up=False))
    sup.book.ingest(DEMO_SHARP_LINES)
    sup.feed.connected.set()
    sup.orders_channel_confirmed = confirmed
    sup.record_live_plan()
    sup._ledger("SESSION_START", max_stake_usd=10.0, exposure_limit_usd=100.0, maker=False)
    return sup, ledger


def dash(ledger):
    return db.snapshot(db.LedgerStore(ledger).refresh(), pd.DataFrame(columns=["ts", "level", "logger", "msg"]))


async def test_dashboard_exposure_matches_engine_at_every_step(tmp_path):
    from main_supervisor import DEMO_MARKETS
    from novig_feed import MarketUpdate
    from novig_private import FillSlip
    info = {m["outcome_id"]: m for m in DEMO_MARKETS}
    sup, ledger = engine_and_ledger(tmp_path)

    def check():
        s = dash(ledger)
        assert s["exposure"] == pytest.approx(sup.total_exposure(), abs=0.01)
        return s

    await sup.on_market_update(MarketUpdate(**info["O-NYK"], price=0.49, available_volume=5000), None)
    s = check()                                                       # reserved, filling
    assert list(s["positions"]["Status"]) == ["FILLING"] and s["positions"]["Contracts"].iloc[0] == 0
    await sup.on_fill_slip(FillSlip(order_id="ex-1", status="PARTIAL", filled_volume=12, price_cents=49))
    s = check()
    assert s["positions"]["Contracts"].iloc[0] == 12 and s["positions"]["Entry Price"].iloc[0] == "49.0¢"
    await sup.on_fill_slip(FillSlip(order_id="ex-1", status="FILLED", filled_volume=20, price_cents=49))
    s = check()
    row = s["positions"].iloc[0]
    assert (row["Status"], row["Contracts"], row["Exposure $"], row["Taker Fill Window"]) == ("DONE", 20, 9.8, "closed")
    await sup.on_market_update(MarketUpdate(**info["O-OVER"], price=0.44, available_volume=5000), None)
    check()                                                           # second game reserved
    assert s["volume_30d"] == pytest.approx(9.80) and dash(ledger)["limits"]["exposure_limit_usd"] == 100.0


async def test_dashboard_matches_engine_on_timeout_and_unconfirmed(tmp_path):
    from main_supervisor import DEMO_MARKETS
    from novig_feed import MarketUpdate
    from novig_private import FillSlip
    info = {m["outcome_id"]: m for m in DEMO_MARKETS}

    sup, ledger = engine_and_ledger(tmp_path / "a", timeout=0.05)     # confirmed channel, partial then timeout
    await sup.on_market_update(MarketUpdate(**info["O-NYK"], price=0.49, available_volume=5000), None)
    await sup.on_fill_slip(FillSlip(order_id="ex-1", status="PARTIAL", filled_volume=5, price_cents=49))
    await asyncio.sleep(0.15)
    s = dash(ledger)
    assert s["exposure"] == pytest.approx(sup.total_exposure()) == pytest.approx(2.45)

    sup, ledger = engine_and_ledger(tmp_path / "b", confirmed=False, timeout=0.05)   # unproven channel
    await sup.on_market_update(MarketUpdate(**info["O-NYK"], price=0.49, available_volume=5000), None)
    await asyncio.sleep(0.15)
    s = dash(ledger)
    assert s["exposure"] == pytest.approx(sup.total_exposure()) == pytest.approx(9.80)
    assert list(s["positions"]["Status"]) == ["UNCONFIRMED"]
    assert s["audit"]["severity"].iloc[0] == "crimson" and "UNCONFIRMED" in s["audit"]["text"].iloc[0]


def test_new_session_resets_exposure_view(tmp_path):
    f = tmp_path / "l.jsonl"
    rows = [{"ts": 1, "event": "SESSION_START"},
            {"ts": 2, "event": "ORDER", "exchange_order_id": "a", "kind": "DIRECTIONAL", "reserved_usd": 10,
             "payload": {"outcomeId": "O"}},
            {"ts": 3, "event": "SESSION_START"}]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert dash(f)["exposure"] == 0.0


# ================================================================ settlement, heartbeat, audit
def test_capital_and_curve_from_settle_events(tmp_path):
    f = tmp_path / "l.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": 100, "event": "SETTLE", "pnl_usd": 12.5}, {"ts": 200, "event": "SETTLE", "pnl_usd": -2.5}]))
    s = dash(f)
    assert s["capital"] == 100_010.0 and s["has_settlements"] and list(s["curve"]["cum_pnl"]) == [12.5, 10.0]
    empty = dash(tmp_path / "missing.jsonl")
    assert empty["capital"] == 100_000.0 and not empty["has_settlements"] and empty["curve"].empty


def log_frame(lines):
    return db.parse_log_chunk("\n".join(lines) + "\n")


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def test_heartbeat_green_only_when_fresh_and_connected():
    now = datetime(2026, 9, 23, 12, 0, 0)
    ok = log_frame([f"{stamp(now - timedelta(seconds=20))} | INFO    | trading.supervisor   | CONN_STATE novig CONNECTED {{}}",
                    f"{stamp(now - timedelta(seconds=3))} | INFO    | trading.supervisor   | HEARTBEAT uptime=5.0s"])
    assert db.heartbeat(ok, now)["ok"] is True
    assert db.heartbeat(ok, now + timedelta(seconds=30))["label"] == "HEARTBEAT STALE"
    down = log_frame([f"{stamp(now - timedelta(seconds=3))} | INFO    | trading.supervisor   | HEARTBEAT uptime=5.0s",
                      f"{stamp(now - timedelta(seconds=1))} | INFO    | trading.supervisor   | CONN_STATE novig DISCONNECTED {{}}"])
    hb = db.heartbeat(down, now)
    assert hb["ok"] is False and hb["label"] == "SOCKET DISCONNECTED"
    assert db.heartbeat(pd.DataFrame(columns=["ts", "level", "logger", "msg"]), now)["label"] == "NO ENGINE LOG"


def test_audit_highlights_kill_switch_timings():
    now = datetime.now()
    log = log_frame([
        f"{stamp(now - timedelta(seconds=3))} | WARNING | trading.execution    | MAKER_KILL bulk-cancelled 4 quote(s) in 3.2ms (budget 200ms): novig websocket drop",
        f"{stamp(now - timedelta(seconds=2))} | ERROR   | trading.execution    | MAKER_KILL bulk-cancelled 4 quote(s) in 350.0ms (budget 200ms): sharp move",
        f"{stamp(now - timedelta(seconds=1))} | INFO    | trading.supervisor   | FEED_UPDATE boring line",
    ])
    feed = db.audit_feed(pd.DataFrame(columns=["ts", "event"]), log)
    assert list(feed["severity"]) == ["crimson", "amber"]             # newest first; boring INFO excluded
    assert feed["text"].iloc[1].startswith("✓ AUTO-CANCEL within 200ms budget")


def test_audit_same_millisecond_events_keep_file_order():
    rows = pd.DataFrame([{"ts": 5.0, "event": "CANCEL"}, {"ts": 5.0, "event": "UNCONFIRMED"}])
    feed = db.audit_feed(rows, pd.DataFrame(columns=["ts", "level", "logger", "msg"]))
    assert list(feed["text"].str.split().str[0]) == ["UNCONFIRMED", "CANCEL"]


def test_audit_rows_show_settle_pnl_and_cancelled_ids():
    rows = pd.DataFrame([{"ts": 1.0, "event": "SETTLE", "pnl_usd": 1.5},
                         {"ts": 2.0, "event": "CANCEL", "exchange_order_ids": ["a", "b"], "reason": "drop"},
                         {"ts": 3.0, "event": "SLIP", "order_id": "srv-9", "status": "FILLED"}])
    feed = db.audit_feed(rows, pd.DataFrame(columns=["ts", "level", "logger", "msg"]))
    assert list(feed["text"]) == ["SLIP  srv-9 FILLED", "CANCEL  a,b drop", "SETTLE  +1.50 USD"]


def test_audit_caps_at_15_rows_newest_first():
    rows = pd.DataFrame({"ts": [time.time() + i for i in range(40)], "event": ["ORDER"] * 40})
    feed = db.audit_feed(rows, pd.DataFrame(columns=["ts", "level", "logger", "msg"]))
    assert len(feed) == 15 and feed["time"].is_monotonic_decreasing


def test_exposure_limit_read_from_ledger():
    rows = pd.DataFrame([{"ts": 1, "event": "SCALE_UP_AUTHORIZED", "exposure_limit_usd": 15000.0,
                          "max_stake_usd": 1000.0, "approved_by": "PM"},
                         {"ts": 2, "event": "SESSION_START", "exposure_limit_usd": 15000.0, "max_stake_usd": 1000.0}])
    lim = db.active_limits(rows)
    assert (lim["exposure_limit_usd"], lim["scaled_up"], lim["approved_by"]) == (15000.0, True, "PM")


# ================================================================ the Streamlit app renders
def test_streamlit_app_renders_headless(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    ledger, log = tmp_path / "live_ledger.jsonl", tmp_path / "trading_engine.log"
    now = datetime.now()
    ledger.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": time.time() - 5, "event": "CANARY_LIMITS", "max_stake_usd": 10, "exposure_limit_usd": 100},
        {"ts": time.time() - 4, "event": "SESSION_START", "max_stake_usd": 10, "exposure_limit_usd": 100},
        {"ts": time.time() - 3, "event": "ORDER", "exchange_order_id": "ex-1", "kind": "DIRECTIONAL",
         "canonical_side": "New York Knicks", "reserved_usd": 9.8, "payload": {"outcomeId": "O-NYK", "side": "buy"}},
        {"ts": time.time() - 2, "event": "FILL", "exchange_order_id": "ex-1", "delta": 20, "filled_total": 20,
         "price": 0.49, "cost_total_usd": 9.8},
        {"ts": time.time() - 1, "event": "DONE", "exchange_order_id": "ex-1", "filled": 20, "cost_usd": 9.8}]))
    log.write_text(f"{stamp(now)} | INFO    | trading.supervisor   | CONN_STATE novig CONNECTED {{}}\n"
                   f"{stamp(now)} | INFO    | trading.supervisor   | HEARTBEAT uptime=5.0s\n")
    monkeypatch.setenv("LEDGER_PATH", str(ledger))
    monkeypatch.setenv("ENGINE_LOG_PATH", str(log))
    at = AppTest.from_file(str(Path(db.__file__)), default_timeout=30).run()
    assert not at.exception
    html_blocks = " ".join(m.value for m in at.markdown)
    for needle in ("Active Capital State", "$100,000.00", "30-Day Filled Volume", "$9.80",
                   "Active Unsettled Exposure", "10% of $100 active limit", "ONLINE", "Real-Time Safety Audit"):
        assert needle in html_blocks, needle
    assert len(at.dataframe) == 1 and at.dataframe[0].value["Contracts"].iloc[0] == 20
