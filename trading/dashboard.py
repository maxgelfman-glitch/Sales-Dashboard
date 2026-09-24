"""
dashboard.py — Read-only visual cockpit for the trading engine.

    cd trading
    streamlit run dashboard.py                       # http://localhost:8501
    LEDGER_PATH=/path/live_ledger.jsonl ENGINE_LOG_PATH=/path/trading_engine.log streamlit run dashboard.py

ISOLATION
    * Runs in its OWN operating-system process (Streamlit server). It never imports
      engine modules, never opens a socket to an exchange and never writes a file the
      engine reads. Its only inputs are two files the engine already writes:
          live_ledger.jsonl    orders, slips, fills, cancels   (append-only JSON lines)
          trading_engine.log   heartbeats, connection states, kill-switch lines
    * Files are tailed by byte offset: each refresh reads ONLY the bytes appended since
      the previous one (never the whole history), and never consumes a half-written
      final line. The engine log's midnight rotation is detected and followed.
    * For extra headroom on a shared machine, start it at lower CPU priority:
          nice -n 10 streamlit run dashboard.py

DATA HONESTY
    * Settled P&L needs "SETTLE" ledger events ({"event": "SETTLE", "pnl_usd": ...}).
      The engine does not write these yet, so capital shows $100,000 + $0 settled and
      the profit curve says so instead of inventing numbers.
    * Exposure is rebuilt from the ledger exactly as the engine books it for the CURRENT
      session (since the last SESSION_START): pending taker orders count their full
      reservation, finished orders their filled cost, UNCONFIRMED orders keep their
      reservation, maker fills their filled cost.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get("TRADING_LOG_DIR", BASE_DIR / "logs"))
LEDGER_PATH = Path(os.environ.get("LEDGER_PATH", LOG_DIR / "live_ledger.jsonl"))
ENGINE_LOG_PATH = Path(os.environ.get("ENGINE_LOG_PATH", LOG_DIR / "trading_engine.log"))

BASELINE_CAPITAL_USD = 100_000.0
HARD_EXPOSURE_CEILING_USD = 15_000.0
EXPOSURE_WARN_FRACTION = 0.80          # amber above 80% of the active limit, crimson at 100%
TAKER_FILL_WINDOW_S = 2.0
MAKER_CANCEL_BUDGET_MS = 200.0
HEARTBEAT_STALE_S = 15.0               # engine heartbeat every 5s: 3 missed beats = red
REFRESH_S = 1.0
LOG_BOOTSTRAP_BYTES = 512 * 1024       # on first open, read only the tail of a large engine log
AUDIT_ROWS = 15
LOCAL_TZ = datetime.now().astimezone().tzinfo

LOG_LINE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| (?P<level>\w+)\s*\| "
                      r"(?P<logger>\S+)\s*\| (?P<msg>.*)$")
NOTABLE_LOG = re.compile(r"MAKER_KILL|KILL_SWITCH|LIVE_UNCONFIRMED|FILL_UNKNOWN|POSITION_RESIDUAL|CONN .* dropped")

# ---------------------------------------------------------------------------
# Incremental file tailing (pure Python; no Streamlit dependency)
# ---------------------------------------------------------------------------
@dataclass
class FileTail:
    """Reads only newly appended, complete lines. Survives truncation and rotation."""
    path: Path
    bootstrap_bytes: Optional[int] = None      # None = read the whole file on first open
    offset: int = 0
    inode: Optional[int] = None
    resets: int = 0                            # bumps when the file was rotated / truncated / replaced
    _partial: bytes = b""
    _skip_to_line: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def read_new(self) -> str:
        with self._lock:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                return ""
            if self.inode is None:                                   # first open
                self.inode = st.st_ino
                if self.bootstrap_bytes is not None and st.st_size > self.bootstrap_bytes:
                    self.offset = st.st_size - self.bootstrap_bytes
                    self._skip_to_line = True
            elif st.st_ino != self.inode or st.st_size < self.offset:   # rotated or truncated
                self.inode, self.offset, self._partial = st.st_ino, 0, b""
                self.resets += 1
            if st.st_size == self.offset:
                return ""
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                data = fh.read(st.st_size - self.offset)
            self.offset += len(data)
            data = self._partial + data
            if self._skip_to_line:                                  # started mid-file: drop the cut line
                self._skip_to_line = False
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else b""
            end = data.rfind(b"\n")
            if end < 0:                                             # no complete line yet
                self._partial = data
                return ""
            self._partial = data[end + 1:]
            return data[: end + 1].decode("utf-8", errors="replace")


LEDGER_COLUMNS = ["ts", "event"]


def parse_ledger_chunk(text: str) -> pd.DataFrame:
    """New ledger lines -> DataFrame (pandas' line-delimited JSON reader; bad lines skipped)."""
    if not text.strip():
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    try:
        df = pd.read_json(StringIO(text), lines=True, dtype=False, convert_dates=False)
    except ValueError:                                              # one corrupt line: parse line by line
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        df = pd.DataFrame.from_records(rows)
    if df.empty or "event" not in df.columns or "ts" not in df.columns:
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    df = df[df["event"].notna() & pd.to_numeric(df["ts"], errors="coerce").notna()]
    return df.reset_index(drop=True)


def parse_log_chunk(text: str) -> pd.DataFrame:
    """Engine log lines -> DataFrame[ts (local naive), level, logger, msg] (vectorized regex)."""
    if not text:
        return pd.DataFrame(columns=["ts", "level", "logger", "msg"])
    s = pd.Series(text.splitlines(), dtype="string")
    parts = s.str.extract(LOG_LINE)
    parts = parts.dropna(subset=["ts"])
    parts["ts"] = pd.to_datetime(parts["ts"], format="%Y-%m-%d %H:%M:%S.%f", errors="coerce")
    return parts.dropna(subset=["ts"]).reset_index(drop=True)


class LedgerStore:
    """Accumulates parsed ledger chunks; concatenates only when something new arrived."""

    def __init__(self, path: Path) -> None:
        self.tail = FileTail(path)
        self._chunks: list[pd.DataFrame] = []
        self._frame = pd.DataFrame(columns=LEDGER_COLUMNS)
        self._dirty = False
        self._seen_resets = 0
        self._lock = threading.Lock()

    def refresh(self) -> pd.DataFrame:
        text = self.tail.read_new()
        chunk = parse_ledger_chunk(text)
        with self._lock:
            if self.tail.resets != self._seen_resets:               # ledger replaced: start over, no duplicates
                self._seen_resets = self.tail.resets
                self._chunks, self._frame, self._dirty = [], pd.DataFrame(columns=LEDGER_COLUMNS), True
            if not chunk.empty:
                self._chunks.append(chunk)
                self._dirty = True
            if self._dirty:
                self._frame = pd.concat(self._chunks, ignore_index=True) if self._chunks else self._frame
                self._chunks = [self._frame]                        # keep a single consolidated chunk
                self._dirty = False
            return self._frame


class LogStore:
    """Keeps only what the cockpit needs from the (large) engine log: bounded recent lines."""

    KEEP = 5000

    def __init__(self, path: Path) -> None:
        self.tail = FileTail(path, bootstrap_bytes=LOG_BOOTSTRAP_BYTES)
        self._frame = pd.DataFrame(columns=["ts", "level", "logger", "msg"])
        self._lock = threading.Lock()

    def refresh(self) -> pd.DataFrame:
        chunk = parse_log_chunk(self.tail.read_new())
        with self._lock:
            if not chunk.empty:
                # keep heartbeat/connection/notable lines only: everything the cockpit reads
                keep = chunk["msg"].str.contains(r"HEARTBEAT uptime|CONN_STATE novig|CONN novig", regex=True) | \
                    chunk["level"].isin(["WARNING", "ERROR", "CRITICAL"]) | chunk["msg"].str.contains(NOTABLE_LOG)
                self._frame = pd.concat([self._frame, chunk[keep]], ignore_index=True).tail(self.KEEP)
            return self._frame


# ---------------------------------------------------------------------------
# Analytics (pure, vectorized; unit-tested without Streamlit)
# ---------------------------------------------------------------------------
def _col(df: pd.DataFrame, name: str, default=np.nan) -> pd.Series:
    return df[name] if name in df.columns else pd.Series(default, index=df.index)


def current_session(ledger: pd.DataFrame) -> pd.DataFrame:
    """Rows since the most recent SESSION_START (the engine forgets state on restart)."""
    if ledger.empty:
        return ledger
    starts = ledger.index[ledger["event"].eq("SESSION_START")]
    return ledger.loc[starts[-1]:] if len(starts) else ledger


def active_limits(ledger: pd.DataFrame) -> dict:
    limits = {"max_stake_usd": None, "exposure_limit_usd": HARD_EXPOSURE_CEILING_USD, "maker": None,
              "scaled_up": False, "approved_by": None}
    if ledger.empty:
        return limits
    rows = ledger[ledger["event"].isin(["SESSION_START", "CANARY_LIMITS", "SCALE_UP_AUTHORIZED"])]
    if rows.empty:
        return limits
    last = rows.iloc[-1]                     # the engine writes the limits line right before SESSION_START
    for key in ("max_stake_usd", "exposure_limit_usd"):
        if key in rows.columns and pd.notna(last.get(key)):
            limits[key] = float(last[key])
    limits["maker"] = last.get("maker") if "maker" in rows.columns else None
    plan = rows[rows["event"].isin(["CANARY_LIMITS", "SCALE_UP_AUTHORIZED"])]
    if not plan.empty and plan["event"].iloc[-1] == "SCALE_UP_AUTHORIZED":
        limits["scaled_up"] = True
        limits["approved_by"] = plan["approved_by"].iloc[-1] if "approved_by" in plan.columns else None
    return limits


def order_book_state(session: pd.DataFrame, now: float) -> pd.DataFrame:
    """One row per live order with status, contracts, entry price and current exposure."""
    cols = ["order_id", "kind", "outcome", "side", "ts", "reserved_usd", "contracts", "cost_usd", "status",
            "exposure_usd", "window"]
    if session.empty or not session["event"].isin(["ORDER", "RESTORE"]).any():
        return pd.DataFrame(columns=cols)
    if not session["event"].eq("ORDER").any():
        return apply_reconciliation(session, pd.DataFrame(columns=cols + ["quote_side"]))[cols]
    orders = session[session["event"].eq("ORDER")]
    payload = _col(orders, "payload", None)
    out = pd.DataFrame({
        "order_id": _col(orders, "exchange_order_id").astype("string"),
        "kind": _col(orders, "kind", "DIRECTIONAL").fillna("DIRECTIONAL").astype("string"),
        "outcome": payload.map(lambda p: p.get("outcomeId") if isinstance(p, dict) else None).astype("string"),
        "quote_side": payload.map(lambda p: p.get("side") if isinstance(p, dict) else None).astype("string"),
        "side": _col(orders, "canonical_side", None).astype("string"),
        "ts": orders["ts"].astype(float),
        "reserved_usd": pd.to_numeric(_col(orders, "reserved_usd", 0.0), errors="coerce").fillna(0.0),
    }).drop_duplicates("order_id", keep="last").set_index("order_id")

    fills = session[session["event"].eq("FILL")]
    if not fills.empty:
        last_fill = fills.groupby(fills["exchange_order_id"].astype("string")).agg(
            contracts=("filled_total", "last"), cost_usd=("cost_total_usd", "last"))
        out = out.join(last_fill)
    else:
        out["contracts"], out["cost_usd"] = np.nan, np.nan
    # DONE closes a whole leg (a staggered leg lists its tranche ids comma-separated); TRANCHE_DONE closes one tranche
    done_ids = set(session.loc[session["event"].isin(["DONE", "TRANCHE_DONE"]), "exchange_order_id"]
                   .astype("string").str.split(",").explode().str.strip()) \
        if "exchange_order_id" in session.columns else set()
    unconf_ids = set(session.loc[session["event"].eq("UNCONFIRMED"), "exchange_order_id"].astype("string")) \
        if "exchange_order_id" in session.columns else set()
    idx = out.index.to_series()
    is_maker = out["kind"].eq("MAKER").to_numpy()
    filled = out["contracts"].fillna(0).to_numpy(dtype=float)
    unconf = idx.isin(unconf_ids).to_numpy()
    done = idx.isin(done_ids).to_numpy()
    out["contracts"] = filled
    out["cost_usd"] = out["cost_usd"].fillna(0.0)
    out["status"] = np.select(
        [is_maker & (filled > 0), is_maker, unconf & (filled <= 0), done, ~done],
        ["MAKER FILL", "QUOTE", "UNCONFIRMED", "DONE", "FILLING"], default="DONE")
    out["exposure_usd"] = np.select(
        [is_maker, out["status"].eq("UNCONFIRMED").to_numpy(), out["status"].eq("FILLING").to_numpy()],
        [out["cost_usd"].to_numpy(), out["reserved_usd"].to_numpy(),
         np.maximum(out["reserved_usd"].to_numpy(), out["cost_usd"].to_numpy())],
        default=out["cost_usd"].to_numpy())
    left = TAKER_FILL_WINDOW_S - (now - out["ts"].to_numpy())
    out["window"] = np.where(is_maker, "resting quote",
                             np.where(out["status"].eq("FILLING").to_numpy(),
                                      np.where(left > 0, pd.Series(left).map("{:.1f}s left".format).to_numpy(),
                                               "cancel pending"),
                                      np.where(out["status"].eq("UNCONFIRMED").to_numpy(), "UNCONFIRMED", "closed")))
    out = out.reset_index()
    return apply_reconciliation(session, out)[cols]


def _ids(session: pd.DataFrame, event: str, column: str) -> set[str]:
    """All ids listed (scalar or list) in `column` of `event` rows."""
    if column not in session.columns:
        return set()
    vals = session.loc[session["event"].eq(event), column].explode().dropna()
    return set(vals.astype("string"))


def apply_reconciliation(session: pd.DataFrame, book: pd.DataFrame) -> pd.DataFrame:
    """Apply RESTORE / SETTLE / UNCONFIRMED_* ledger rows so exposure matches the engine's own ledger of record."""
    settled_orders = _ids(session, "SETTLE", "released_order_ids")
    settled_positions = _ids(session, "SETTLE", "released_position_ids")
    not_filled = _ids(session, "UNCONFIRMED_RELEASED", "exchange_order_id")
    resolved = session[session["event"].eq("UNCONFIRMED_RESOLVED")]
    if not resolved.empty and not book.empty:
        r = resolved.drop_duplicates("exchange_order_id", keep="last").set_index(
            resolved["exchange_order_id"].astype("string"))
        hit = book["order_id"].isin(r.index)
        ids = book.loc[hit, "order_id"]
        book.loc[hit, "contracts"] = pd.to_numeric(r.loc[ids, "filled"], errors="coerce").to_numpy()
        book.loc[hit, "cost_usd"] = pd.to_numeric(r.loc[ids, "exposure_usd"], errors="coerce").to_numpy()
        book.loc[hit, "exposure_usd"] = book.loc[hit, "cost_usd"]
        book.loc[hit, "status"], book.loc[hit, "window"] = "RESOLVED", "closed"
    if not book.empty:
        gone = book["order_id"].isin(settled_orders)
        book.loc[gone, ["exposure_usd"]] = 0.0
        book.loc[gone, "status"], book.loc[gone, "window"] = "SETTLED", "closed"
        nf = book["order_id"].isin(not_filled)
        book.loc[nf, ["exposure_usd"]] = 0.0
        book.loc[nf, "status"], book.loc[nf, "window"] = "NOT FILLED", "closed"
    restores = session[session["event"].eq("RESTORE")]
    if not restores.empty:
        rid = _col(restores, "restore_id").astype("string")
        cost = pd.to_numeric(_col(restores, "exposure_usd", 0.0), errors="coerce").fillna(0.0)
        is_settled = rid.isin(settled_positions)
        extra = pd.DataFrame({
            "order_id": rid, "kind": "RESTORED", "outcome": _col(restores, "outcome_id").astype("string"),
            "side": _col(restores, "side").astype("string"), "ts": restores["ts"].astype(float),
            "reserved_usd": 0.0, "contracts": pd.to_numeric(_col(restores, "contracts", 0), errors="coerce").fillna(0),
            "cost_usd": cost, "status": np.where(is_settled, "SETTLED", "RESTORED"),
            "exposure_usd": np.where(is_settled, 0.0, cost), "window": "restored at sync"})
        book = pd.concat([book, extra], ignore_index=True) if not book.empty else extra
    return book


def open_positions(book: pd.DataFrame) -> pd.DataFrame:
    """What the table shows: anything holding contracts or still reserving capital."""
    if book.empty:
        return pd.DataFrame(columns=["Game / Outcome ID", "Side", "Exchange Pool", "Contracts", "Entry Price",
                                     "Exposure $", "Taker Fill Window", "Status"])
    rows = book[((book["contracts"] > 0) | book["status"].isin(["FILLING", "UNCONFIRMED", "RESTORED"]))
                & ~book["status"].isin(["SETTLED", "NOT FILLED"])]
    entry = np.where(rows["contracts"] > 0, rows["cost_usd"] / rows["contracts"].where(rows["contracts"] > 0), np.nan)
    return pd.DataFrame({
        "Game / Outcome ID": rows["outcome"].fillna("?"),
        "Side": rows["side"].fillna(rows["quote_side"]) if "quote_side" in rows else rows["side"].fillna("?"),
        "Exchange Pool": np.where(rows["status"].eq("RESTORED"), "Novig (restored)", "Novig (live)"),
        "Contracts": rows["contracts"].astype(int),
        "Entry Price": pd.Series(entry, index=rows.index).map(lambda v: "—" if np.isnan(v) else f"{v * 100:.1f}¢"),
        "Exposure $": rows["exposure_usd"].round(2),
        "Taker Fill Window": rows["window"],
        "Status": rows["status"],
    }).reset_index(drop=True)


def rolling_volume(ledger: pd.DataFrame, now: float, days: int = 30) -> float:
    if ledger.empty or not ledger["event"].eq("FILL").any():
        return 0.0
    fills = ledger[ledger["event"].eq("FILL") & (ledger["ts"].astype(float) >= now - days * 86400)]
    return float((pd.to_numeric(fills["delta"], errors="coerce") * pd.to_numeric(fills["price"], errors="coerce"))
                 .fillna(0.0).sum())


def settled_curve(ledger: pd.DataFrame) -> pd.DataFrame:
    """Cumulative settled net P&L from SETTLE rows (net_profit_usd; pnl_usd accepted). Empty if none."""
    if ledger.empty or not ledger["event"].eq("SETTLE").any():
        return pd.DataFrame(columns=["time", "cum_pnl", "capital", "unknown"])
    s = ledger[ledger["event"].eq("SETTLE")]
    net = pd.to_numeric(_col(s, "net_profit_usd"), errors="coerce")
    net = net.fillna(pd.to_numeric(_col(s, "pnl_usd"), errors="coerce"))
    cum = net.fillna(0.0).cumsum()
    pool = pd.to_numeric(_col(s, "resulting_capital_pool"), errors="coerce")
    return pd.DataFrame({"time": pd.to_datetime(s["ts"].astype(float), unit="s", utc=True)
                                 .dt.tz_convert(LOCAL_TZ).dt.tz_localize(None),
                         "cum_pnl": cum, "capital": pool.fillna(BASELINE_CAPITAL_USD + cum),
                         "unknown": net.isna()})


def heartbeat(log: pd.DataFrame, now_local: datetime) -> dict:
    """Green only if the engine logged a heartbeat recently AND the Novig socket's last state is CONNECTED."""
    if log.empty:
        return {"ok": False, "label": "NO ENGINE LOG", "age_s": None, "socket": "unknown"}
    beats = log[log["msg"].str.startswith("HEARTBEAT uptime", na=False)]
    conn = log[log["msg"].str.contains(r"^CONN_STATE novig (?:CONNECTED|DISCONNECTED|STOPPED|CONNECTING)",
                                       regex=True, na=False)]
    age = (now_local - beats["ts"].iloc[-1]).total_seconds() if not beats.empty else None
    socket = conn["msg"].iloc[-1].split()[2] if not conn.empty else "unknown"
    ok = age is not None and age <= HEARTBEAT_STALE_S and socket == "CONNECTED"
    label = "ONLINE" if ok else ("SOCKET " + socket if socket != "CONNECTED" else "HEARTBEAT STALE")
    return {"ok": ok, "label": label, "age_s": age, "socket": socket}


SEVERITY_ORDER = {"crimson": 2, "amber": 1, "normal": 0}


def audit_feed(ledger: pd.DataFrame, log: pd.DataFrame, rows: int = AUDIT_ROWS) -> pd.DataFrame:
    """Latest ledger events + notable engine-log lines, newest first, with a severity colour."""
    parts = []
    if not ledger.empty:
        tail = ledger.tail(rows * 2)
        ev = tail["event"].astype("string")
        net = pd.to_numeric(_col(tail, "net_profit_usd"), errors="coerce").map(
            lambda v: "" if pd.isna(v) else f"net {v:+,.2f} USD")
        ids = _col(tail, "exchange_order_ids", None).map(lambda v: ",".join(map(str, v)) if isinstance(v, list) else "")
        pnl = pd.to_numeric(_col(tail, "pnl_usd"), errors="coerce").map(lambda v: "" if pd.isna(v) else f"{v:+,.2f} USD")
        detail = (_col(tail, "kind", "").fillna("").astype("string") + " " +
                  _col(tail, "exchange_order_id", "").fillna("").astype("string") + " " +
                  _col(tail, "order_id", "").fillna("").astype("string") + " " +
                  _col(tail, "status", "").fillna("").astype("string") + " " + ids.astype("string") + " " +
                  pnl.astype("string") + " " + net.astype("string") + " " +
                  _col(tail, "outcome_id", "").fillna("").astype("string") + " " +
                  _col(tail, "reason", "").fillna("").astype("string")).str.replace(r"\s+", " ", regex=True).str.strip()
        sev = np.select([ev.isin(["UNCONFIRMED", "REJECTED", "CANCEL_FAILED", "SCALE_UP_AUTHORIZED", "SYNC_FAILED",
                                  "POSITION_MISMATCH"]),
                         ev.isin(["CANCEL", "CANARY_LIMITS", "RESTORE", "UNCONFIRMED_RELEASED",
                                  "UNCONFIRMED_RESOLVED"])], ["crimson", "amber"], default="normal")
        parts.append(pd.DataFrame({
            # ledger ts is UTC epoch; engine-log ts is local wall time -> compare in local time
            "time": pd.to_datetime(tail["ts"].astype(float), unit="s", utc=True)
                      .dt.tz_convert(LOCAL_TZ).dt.tz_localize(None),
            "source": "ledger", "text": ev + "  " + detail, "severity": sev,
            "seq": np.arange(len(tail))}))
    if not log.empty:
        notable = log[log["level"].isin(["WARNING", "ERROR", "CRITICAL"]) | log["msg"].str.contains(NOTABLE_LOG)]
        notable = notable.tail(rows * 2)
        kill_ms = pd.to_numeric(notable["msg"].str.extract(r"MAKER_KILL .* in ([\d.]+)ms")[0], errors="coerce")
        sev = np.select([notable["level"].isin(["ERROR", "CRITICAL"]) | (kill_ms > MAKER_CANCEL_BUDGET_MS),
                         kill_ms.notna() | notable["level"].eq("WARNING")], ["crimson", "amber"], default="normal")
        text = np.where(kill_ms.notna() & (kill_ms <= MAKER_CANCEL_BUDGET_MS),
                        "✓ AUTO-CANCEL within " + f"{MAKER_CANCEL_BUDGET_MS:.0f}ms budget — " + notable["msg"],
                        notable["level"] + "  " + notable["msg"])
        parts.append(pd.DataFrame({"time": notable["ts"], "source": "engine", "text": text, "severity": sev,
                                   "seq": np.arange(len(notable))}))
    if not parts:
        return pd.DataFrame(columns=["time", "source", "text", "severity"])
    # same-millisecond events keep their file order (later line = newer), so ties break on sequence
    feed = pd.concat(parts, ignore_index=True).sort_values(["time", "seq"], ascending=False, kind="stable")
    return feed.head(rows).drop(columns="seq").reset_index(drop=True)


def snapshot(ledger: pd.DataFrame, log: pd.DataFrame, now: Optional[float] = None,
             now_local: Optional[datetime] = None) -> dict:
    """Everything the cockpit renders, computed in one pass."""
    now = time.time() if now is None else now
    now_local = datetime.now() if now_local is None else now_local
    session = current_session(ledger)
    book = order_book_state(session, now)
    curve = settled_curve(ledger)
    limits = active_limits(ledger)
    return {
        # the engine's own running pool (resulting_capital_pool) is authoritative when present
        "capital": float(curve["capital"].iloc[-1]) if not curve.empty else BASELINE_CAPITAL_USD,
        "unknown_pnl_settlements": int(curve["unknown"].sum()) if not curve.empty else 0,
        "settled_pnl": float(curve["cum_pnl"].iloc[-1]) if not curve.empty else 0.0,
        "has_settlements": not curve.empty,
        "volume_30d": rolling_volume(ledger, now),
        "exposure": float(book["exposure_usd"].sum()) if not book.empty else 0.0,
        "limits": limits,
        "heartbeat": heartbeat(log, now_local),
        "positions": open_positions(book),
        "curve": curve,
        "audit": audit_feed(ledger, log),
        "ledger_rows": len(ledger),
    }


# ---------------------------------------------------------------------------
# Rendering (Streamlit / Plotly only below this line)
# ---------------------------------------------------------------------------
CSS = """
<style>
:root { --bg:#07090d; --panel:#0e131b; --edge:#1d2633; --text:#e6edf3; --muted:#7d8a99;
        --green:#00e676; --amber:#ffb300; --crimson:#ff1744; }
.stApp { background: var(--bg); color: var(--text); }
.block-container { padding-top: 3.2rem; max-width: 1600px; }
h1, h2, h3 { color: var(--text); letter-spacing: .02em; }
.kpi { background: var(--panel); border: 1px solid var(--edge); border-radius: 10px; padding: 14px 18px; height: 118px; }
.kpi .label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; }
.kpi .value { font: 700 1.9rem/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; margin-top: 6px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi .value.compact { font-size: 1.45rem; }
.kpi .sub { color: var(--muted); font-size: .75rem; margin-top: 4px; }
.ok { color: var(--green); } .warn { color: var(--amber); } .bad { color: var(--crimson); }
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .35 } }
.pulse { animation: pulse 1.2s ease-in-out infinite; }
.audit { background: var(--panel); border: 1px solid var(--edge); border-radius: 10px; padding: 8px 12px;
         font: .78rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; height: 470px; overflow-y: auto; }
.audit .row { padding: 3px 0; border-bottom: 1px solid #141b25; white-space: nowrap; overflow: hidden;
              text-overflow: ellipsis; }
.audit .t { color: var(--muted); margin-right: 8px; }
.audit .amber { color: var(--amber); font-weight: 700; } .audit .crimson { color: var(--crimson); font-weight: 700; }
.note { color: var(--muted); font-size: .8rem; }
</style>
"""


def kpi(label: str, value: str, sub: str = "", cls: str = "") -> str:
    """One KPI card (values are pre-formatted numbers or fixed strings, never raw file content)."""
    return (f'<div class="kpi"><div class="label">{html.escape(label)}</div>'
            f'<div class="value {cls}">{value}</div><div class="sub">{sub}</div></div>')


def render(st, go, state: dict) -> None:
    # ---- top row KPIs
    c1, c2, c3, c4 = st.columns(4)
    cap_sub = (f"baseline $100,000 {state['settled_pnl']:+,.2f} settled" if state["has_settlements"]
               else "baseline $100,000 · no SETTLE events in ledger yet")
    if state["unknown_pnl_settlements"]:
        cap_sub += f" · ⚠ {state['unknown_pnl_settlements']} settlement(s) with unknown P&L"
    c1.markdown(kpi("Active Capital State", f"${state['capital']:,.2f}", cap_sub), unsafe_allow_html=True)
    c2.markdown(kpi("30-Day Filled Volume", f"${state['volume_30d']:,.2f}", "sum of filled contracts × price"),
                unsafe_allow_html=True)
    lim = state["limits"]["exposure_limit_usd"] or HARD_EXPOSURE_CEILING_USD
    ratio = state["exposure"] / lim if lim else 0.0
    exp_cls = "bad" if ratio >= 1 else "warn" if ratio >= EXPOSURE_WARN_FRACTION else "ok"
    c3.markdown(kpi("Active Unsettled Exposure", f"${state['exposure']:,.2f}",
                    f"{ratio:.0%} of ${lim:,.0f} active limit · hard ceiling ${HARD_EXPOSURE_CEILING_USD:,.0f}",
                    exp_cls), unsafe_allow_html=True)
    hb = state["heartbeat"]
    icon = "✔" if hb["ok"] else "✖"
    age = "—" if hb["age_s"] is None else f"last beat {hb['age_s']:.0f}s ago"
    c4.markdown(kpi("Global Network Heartbeat", f'<span class="pulse">{icon}</span> {hb["label"]}',
                    f"novig socket: {hb['socket']} · {age}", ("ok" if hb["ok"] else "bad") + " compact"),
                unsafe_allow_html=True)

    # ---- centre: cumulative net profit curve
    curve = state["curve"]
    fig = go.Figure()
    if curve.empty:
        fig.add_trace(go.Scatter(x=[datetime.now() - timedelta(hours=1), datetime.now()],
                                 y=[0, 0], mode="lines", line=dict(color="#00e676", width=2),
                                 fill="tozeroy", fillcolor="rgba(0,230,118,0.10)", name="net P&L"))
        fig.add_annotation(text="No SETTLE events in the ledger yet — the curve starts when results are recorded",
                           showarrow=False, font=dict(color="#7d8a99", size=13), xref="paper", yref="paper",
                           x=0.5, y=0.6)
    else:
        fig.add_trace(go.Scatter(x=curve["time"], y=curve["cum_pnl"], mode="lines", name="cumulative net P&L",
                                 line=dict(color="#00e676", width=2.5, shape="hv"),
                                 fill="tozeroy", fillgradient=dict(type="vertical",
                                                                  colorscale=[[0, "rgba(0,230,118,0.0)"],
                                                                              [1, "rgba(0,230,118,0.35)"]])))
    fig.update_layout(template="plotly_dark", paper_bgcolor="#0e131b", plot_bgcolor="#0e131b", height=340,
                      margin=dict(l=10, r=10, t=36, b=10), title="Cumulative Net Profit (settled)",
                      yaxis=dict(title="net wallet growth ($)", gridcolor="#1d2633", zerolinecolor="#2b3747"),
                      xaxis=dict(title="time (local)", gridcolor="#1d2633"), showlegend=False)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    # ---- split columns
    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Active Positions")
        pos = state["positions"]
        if pos.empty:
            st.markdown('<div class="note">No open or filling live orders in the current session.</div>',
                        unsafe_allow_html=True)
        else:
            st.dataframe(pos, hide_index=True, width="stretch", height=min(470, 36 * (len(pos) + 1) + 3))
    with right:
        st.markdown("#### Real-Time Safety Audit")
        rows = []
        for r in state["audit"].itertuples(index=False):
            t = r.time.strftime("%H:%M:%S") if pd.notna(r.time) else "--:--:--"
            cls = "" if r.severity == "normal" else r.severity
            rows.append(f'<div class="row {cls}"><span class="t">{t}</span>{html.escape(str(r.text))}</div>')
        body = "".join(rows) or '<div class="note">Waiting for ledger / engine-log events…</div>'
        st.markdown(f'<div class="audit">{body}</div>', unsafe_allow_html=True)

    limits = state["limits"]
    stake = "—" if limits["max_stake_usd"] is None else f"${limits['max_stake_usd']:,.2f}"
    approval = f" · SCALE-UP approved by {html.escape(str(limits['approved_by']))}" if limits["scaled_up"] else ""
    st.markdown(f'<div class="note">ledger: {html.escape(str(LEDGER_PATH))} · {state["ledger_rows"]} rows · '
                f'engine log: {html.escape(str(ENGINE_LOG_PATH))} · active limits: max stake {stake}, '
                f'exposure ${lim:,.0f}{approval} · refreshed {datetime.now():%H:%M:%S}</div>',
                unsafe_allow_html=True)


def main() -> None:
    import plotly.graph_objects as go
    import streamlit as st

    st.set_page_config(page_title="Trading Cockpit", page_icon="📈", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown("### ⚡ Trading Cockpit <span class='note'>read-only · separate process</span>",
                unsafe_allow_html=True)

    @st.cache_resource
    def stores(ledger_path: str, log_path: str):
        return LedgerStore(Path(ledger_path)), LogStore(Path(log_path))

    ledger_store, log_store = stores(str(LEDGER_PATH), str(ENGINE_LOG_PATH))

    @st.fragment(run_every=REFRESH_S)
    def live() -> None:
        render(st, go, snapshot(ledger_store.refresh(), log_store.refresh()))

    live()


if __name__ == "__main__":
    main()
