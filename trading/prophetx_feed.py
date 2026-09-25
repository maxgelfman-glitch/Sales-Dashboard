"""
prophetx_feed.py — ProphetX as a PRICE FEED (paper trading + research). No live orders from here.

ProphetX (New York-based peer-to-peer exchange; 2% of NET winnings on winning straights) publishes a partner
API. Its documentation site is unreachable from the environment this was built in, so the endpoints below come
from ProphetX's public docs as mirrored by an open-source client (github.com/illgitthat/fastprophetx, MIT):

    auth        POST {base}/auth/login   {"access_key", "secret_key"} -> data.access_token / refresh_token
    tournaments GET  {base}/mm/get_tournaments?has_active_events=true            -> data.tournaments[]
    events      GET  {base}/mm/get_sport_events?tournament_id=...                -> data.sport_events[]
    markets     GET  {base}/v4/mm/get_multiple_markets?event_ids=1,2 (<= 50)     -> data {event_id: [market]}
    market      {"id", "type"/"sub_type"/"name", "status", "strike"?, "market_strikes"?: [...],
                 "selections": [[level, ...], ...]}   one inner list per selection (outcome)
    level       {"strike_id", "outcome_id", "name", "price" (AMERICAN odds), "quantity"}
    base        production https://cash.api.prophetx.co/partner, sandbox https://api.sandbox.prophetx.dev/partner

ASSUMED (verify with `python prophetx_feed.py --probe` once you have API keys; it saves raw responses):
    * a level's price is the odds available TO TAKE on that selection, and quantity is the stake ($) available
      at that price  -> contract price p = implied probability of the odds, contracts = quantity / p
    * event fields: name ("Away at Home" / "Home vs Away"), competitors[] with a home/away flag, scheduled start
    * the bearer token goes in "Authorization: Bearer <token>"
Until those are confirmed ProphetX stays data-only: the supervisor never sends it orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from novig_feed import MAX_BOOK_LEVELS, MarketInfo, MarketRegistry, MarketUpdate, canonical_market_type
from novig_rest import parse_start_time
from team_normalizer import DYNAMIC_LEAGUES, canonical_league, names_match, normalize_outcome, normalize_team_name

log = logging.getLogger("trading.prophetx")

PROD_BASE = "https://cash.api.prophetx.co/partner"
SANDBOX_BASE = "https://api.sandbox.prophetx.dev/partner"
POLL_SECONDS = 3.0
EVENTS_REFRESH_SECONDS = 300.0
LOOKAHEAD_SECONDS = 36 * 3600
TRACKED_TYPES = {"moneyline", "spread", "total"}


def american_to_price(odds: float) -> Optional[float]:
    """American odds -> cost of a $1-payout contract (implied probability). None if invalid."""
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o >= 100:
        return 100.0 / (o + 100.0)
    if o <= -100:
        return -o / (-o + 100.0)
    return None


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


_LINE_RX = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*$")
_MATCHUP_AT = re.compile(r"^\s*(?P<away>.+?)\s+(?:at|@)\s+(?P<home>.+?)\s*$", re.I)
_MATCHUP_VS = re.compile(r"^\s*(?P<home>.+?)\s+(?:vs\.?|v\.?)\s+(?P<away>.+?)\s*$", re.I)


def event_teams(ev: dict) -> Optional[tuple[str, str]]:
    """(home, away) from competitors (home/away flags) or the event name."""
    comps = ev.get("competitors") or ev.get("participants") or []
    home = away = None
    for c in comps if isinstance(comps, list) else []:
        if not isinstance(c, dict):
            continue
        side = str(_first(c, "side", "home_away", "qualifier", "type") or "").lower()
        name = _first(c, "display_name", "name")
        if c.get("is_home") is True or side == "home":
            home = name
        elif c.get("is_away") is True or side == "away":
            away = name
    if home and away:
        return str(home), str(away)
    name = str(_first(ev, "name", "display_name", "event_name") or "")
    for rx in (_MATCHUP_AT, _MATCHUP_VS):
        m = rx.match(name)
        if m:
            return m.group("home").strip(), m.group("away").strip()
    return None


def _containers(market: dict):
    """Yield (container, strike) pairs: the market itself and each of its market_strikes entries."""
    if isinstance(market.get("selections"), list):
        yield market, _first(market, "strike", "line", "points")
    for strike in market.get("market_strikes") or []:
        if isinstance(strike, dict) and isinstance(strike.get("selections"), list):
            yield strike, _first(strike, "strike", "line", "points", "value")


def parse_markets(event: dict, league: str, markets: list[dict], start: Optional[float]) -> list[tuple[MarketInfo, list]]:
    """
    ProphetX markets of one event -> [(MarketInfo, ask_levels)], ask_levels = [(price, contracts)] cheapest first.
    Only moneyline / spread / total full-game markets with exactly two selections are kept.
    """
    teams = event_teams(event)
    if teams is None:
        return []
    raw_home, raw_away = teams
    dynamic = league in DYNAMIC_LEAGUES
    if dynamic:
        home, away = raw_home, raw_away
    else:
        home, away = normalize_team_name(raw_home, league), normalize_team_name(raw_away, league)
        if home is None or away is None:
            return []
    eid = str(_first(event, "event_id", "id", "sport_event_id"))
    out = []
    for m in markets:
        if not isinstance(m, dict) or str(m.get("status", "active")).lower() not in {"active", "open"}:
            continue
        mtype = canonical_market_type(str(_first(m, "type", "sub_type", "name") or ""))
        if mtype not in TRACKED_TYPES:
            continue
        mid = str(_first(m, "id", "market_id"))
        for container, strike in _containers(m):
            sels = [s for s in container["selections"] if isinstance(s, list) and s]
            if len(sels) != 2:
                continue
            rows = []
            for levels in sels:
                first = levels[0] if isinstance(levels[0], dict) else {}
                name = str(_first(first, "display_name", "name") or "")
                line = _first(first, "line", "strike", "points")
                m_line = _LINE_RX.search(name)
                if line is None and m_line and mtype != "moneyline":
                    line = float(m_line.group(1))
                if line is None and strike is not None and mtype != "moneyline":
                    line = float(strike)
                outcome = _LINE_RX.sub("", name).strip() if mtype != "moneyline" else name
                if mtype == "total":
                    canon = normalize_outcome(outcome, league)
                elif dynamic:
                    hits = [t for t in (home, away) if names_match(league, outcome, t)]
                    canon = hits[0] if len(hits) == 1 else None
                else:
                    canon = normalize_team_name(outcome, league)
                asks = []
                for lv in levels:
                    if not isinstance(lv, dict):
                        continue
                    p = american_to_price(lv.get("price"))
                    q = lv.get("quantity")
                    if p is None or not isinstance(q, (int, float)) or q <= 0 or not 0 < p < 1:
                        continue
                    asks.append((round(p, 6), round(float(q) / p, 2)))        # $ stake -> contracts
                asks.sort()
                oid = f"px:{mid}:{_first(first, 'outcome_id', 'strike_id') or name}:{line}"
                rows.append((canon, outcome, None if line is None else float(line), oid, asks,
                             _first(first, "strike_id")))
            if any(r[0] is None for r in rows):
                continue
            for i, (canon, outcome, line, oid, asks, strike_id) in enumerate(rows):
                sib = rows[1 - i][3]
                info = MarketInfo(venue="prophetx", outcome_id=oid, market_id=f"px:{mid}:{strike}", event_id=eid,
                                  sibling_outcome_id=sib, league=league, market_type=mtype, home_team=home,
                                  away_team=away, outcome=canon if mtype == "total" else outcome, line=line,
                                  start_time=start)
                out.append((info, asks[:MAX_BOOK_LEVELS]))
    return out


class ProphetXClient:
    def __init__(self, access_key: str, secret_key: str, base_url: str = PROD_BASE, timeout: float = 10.0) -> None:
        if not (access_key and secret_key):
            raise ValueError("ProphetX needs PROPHETX_ACCESS_KEY and PROPHETX_SECRET_KEY")
        self.base = base_url.rstrip("/")
        self.access_key, self.secret_key = access_key, secret_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.token: Optional[str] = None
        self.token_expires = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def login(self) -> None:
        s = await self._session_get()
        async with s.post(f"{self.base}/auth/login",
                          json={"access_key": self.access_key, "secret_key": self.secret_key}) as r:
            r.raise_for_status()
            data = (await r.json(content_type=None)).get("data") or {}
        self.token = data.get("access_token")
        if not self.token:
            raise RuntimeError("ProphetX login returned no access_token")
        exp = data.get("access_expire_time")
        self.token_expires = float(exp) if isinstance(exp, (int, float)) and exp > 1e9 else time.time() + 600

    async def get(self, path: str, **params) -> Any:
        if self.token is None or time.time() > self.token_expires - 60:
            await self.login()
        s = await self._session_get()
        async with s.get(f"{self.base}/{path}", params={k: v for k, v in params.items() if v is not None},
                         headers={"Authorization": f"Bearer {self.token}"}) as r:
            if r.status == 401:
                self.token = None
            r.raise_for_status()
            return (await r.json(content_type=None)).get("data")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class ProphetXFeed:
    """Polling price feed with the same surface the supervisor uses for the other venues."""

    venue = "prophetx"

    def __init__(self, client: ProphetXClient, on_update: Optional[Callable[..., Awaitable[None]]] = None,
                 on_state_change: Optional[Callable[..., Awaitable[None]]] = None, poll_seconds: float = POLL_SECONDS,
                 clock=time.time) -> None:
        self.client = client
        self.on_update, self.on_state_change = on_update, on_state_change
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.url = client.base
        self.registry = MarketRegistry()
        self.latest: dict[str, MarketUpdate] = {}
        self.connected = asyncio.Event()
        self.events: dict[str, tuple[dict, str, Optional[float]]] = {}   # event_id -> (event, league, start)
        self._events_at = 0.0

    async def _state(self, state: str, **details) -> None:
        if self.on_state_change is not None:
            await self.on_state_change(state, {"venue": "prophetx", **details})

    async def refresh_events(self) -> None:
        events: dict[str, tuple[dict, str, Optional[float]]] = {}
        data = await self.client.get("mm/get_tournaments", has_active_events="true")
        tournaments = data.get("tournaments") if isinstance(data, dict) else data
        for tour in tournaments or []:
            league = canonical_league(str(_first(tour, "name", "display_name", "sport_name") or ""))
            if league not in {"NFL", "NBA", "MLB", "NHL", "WNBA", "NCAAF", "NCAAB", "TENNIS"}:
                continue
            ev_data = await self.client.get("mm/get_sport_events", tournament_id=_first(tour, "id", "tournament_id"))
            for ev in (ev_data.get("sport_events") if isinstance(ev_data, dict) else ev_data) or []:
                start = parse_start_time(_first(ev, "scheduled", "start_time", "scheduled_start", "start"))
                status = str(ev.get("status", "")).lower()
                if status in {"live", "in_progress", "ended", "closed", "cancelled"}:
                    continue
                if start is not None and start - self.clock() > LOOKAHEAD_SECONDS:
                    continue
                events[str(_first(ev, "event_id", "id"))] = (ev, league, start)
        self.events = events
        self._events_at = self.clock()
        log.info("PROPHETX tracking %d upcoming events", len(events))

    async def poll_once(self) -> int:
        if not self.events or self.clock() - self._events_at > EVENTS_REFRESH_SECONDS:
            await self.refresh_events()
        ids = list(self.events)
        infos: list[tuple[MarketInfo, list]] = []
        for i in range(0, len(ids), 50):
            data = await self.client.get("v4/mm/get_multiple_markets", event_ids=",".join(ids[i:i + 50])) or {}
            for eid, markets in (data.items() if isinstance(data, dict) else []):
                if eid in self.events:
                    ev, league, start = self.events[eid]
                    infos += parse_markets(ev, league, markets or [], start)
        self.registry.replace_all([info for info, _ in infos])
        changed = 0
        for info, asks in infos:
            top = dict(price=asks[0][0] if asks else None, available_volume=asks[0][1] if asks else 0.0,
                       ask_levels=asks)
            prev = self.latest.get(info.outcome_id)
            if prev is not None and prev.price == top["price"] and prev.ask_levels == asks:
                continue
            upd = MarketUpdate.from_info(info, **top)
            self.latest[info.outcome_id] = upd
            changed += 1
            if self.on_update is not None:
                await self.on_update(upd, prev)
        return changed

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
                if not self.connected.is_set():
                    self.connected.set()
                    await self._state("CONNECTED")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a failed poll marks the feed down; prices go stale
                log.warning("PROPHETX poll failed (%s: %s)", type(exc).__name__, exc)
                if self.connected.is_set():
                    self.connected.clear()
                    self.latest.clear()
                    await self._state("DISCONNECTED", error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        await self.client.close()


async def _probe(out: Path) -> None:
    client = ProphetXClient(os.environ.get("PROPHETX_ACCESS_KEY", ""), os.environ.get("PROPHETX_SECRET_KEY", ""),
                            os.environ.get("PROPHETX_API_BASE") or PROD_BASE)
    try:
        dump: dict[str, Any] = {"tournaments": await client.get("mm/get_tournaments", has_active_events="true")}
        tours = dump["tournaments"].get("tournaments") if isinstance(dump["tournaments"], dict) else dump["tournaments"]
        dump["events"], dump["markets"] = {}, {}
        for tour in (tours or [])[:3]:
            tid = _first(tour, "id", "tournament_id")
            evs = await client.get("mm/get_sport_events", tournament_id=tid)
            dump["events"][str(tid)] = evs
            lst = evs.get("sport_events") if isinstance(evs, dict) else evs
            ids = [str(_first(e, "event_id", "id")) for e in (lst or [])[:3]]
            if ids:
                dump["markets"][str(tid)] = await client.get("v4/mm/get_multiple_markets", event_ids=",".join(ids))
        out.write_text(json.dumps(dump, indent=2, default=str))
        print(f"saved raw ProphetX responses to {out} — send this file (it contains no credentials) so the "
              f"parser's assumptions can be checked")
    finally:
        await client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ProphetX API probe (read-only)")
    ap.add_argument("--probe", action="store_true", help="save raw tournaments/events/markets responses")
    ap.add_argument("--out", default="prophetx_probe.json")
    a = ap.parse_args()
    if a.probe:
        asyncio.run(_probe(Path(a.out)))
