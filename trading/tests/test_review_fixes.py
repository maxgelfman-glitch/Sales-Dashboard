"""Operational safeguards added after the full adversarial review."""

import asyncio
import time
from pathlib import Path

import pytest

from main_supervisor import Supervisor
from tests.test_live_execution import FakeGateway, live_sup, upd


class GeoRefused(RuntimeError):
    status, code = 451, "GEOLOCATION_EXPIRED"


class ServerError(RuntimeError):
    status = 503


async def test_a_location_refusal_halts_the_venue_until_restart():
    sup = live_sup()

    async def refuse(*a, **k):
        raise GeoRefused("open the app")
    sup.order_gateway.place_limit = refuse
    await sup.on_market_update(upd("O-NYK", 0.49), None)
    assert "location refused" in sup.venue_halted("novig") and not sup._live_ready("novig")
    assert sup.venue_halts["novig"][0] == float("inf")                  # until a human restarts


def test_repeated_server_errors_pause_the_venue_for_five_minutes():
    sup = live_sup()
    for _ in range(3):
        sup.note_order_error("kalshi", ServerError("bad gateway"))
    until, reason = sup.venue_halts["kalshi"]
    assert "server" in reason and 250 < until - time.time() <= 300


def test_no_audit_trail_means_no_new_risk(tmp_path):
    sup = live_sup(ledger_path=tmp_path / "l.jsonl")
    assert sup._live_ready("novig")
    sup.ledger_path = Path("/proc/forbidden/ledger.jsonl")              # unwritable
    sup._ledger("PING")
    assert not sup.ledger_ok and not sup._live_ready("novig") and sup._combo_block_reason() == "ledger not writable"


def test_a_second_engine_cannot_start_on_the_same_account(tmp_path):
    a = live_sup(ledger_path=tmp_path / "l.jsonl")
    b = live_sup(ledger_path=tmp_path / "l.jsonl")
    assert a.acquire_instance_lock()
    assert not b.acquire_instance_lock()
    a._lock_fh.close()                                                  # first engine exits
    assert b.acquire_instance_lock()


def test_thin_leagues_need_a_bigger_edge():
    sup = live_sup()
    assert sup.league_min_edge["NCAAB"] == 0.045 and "NBA" not in sup.league_min_edge


async def test_a_league_minimum_turns_a_small_edge_into_a_pass():
    from execution import SharpQuote
    sup = live_sup()
    q = SharpQuote(odds_for=-110, odds_against=-110)                   # fair 0.50
    nba = await sup._evaluate("novig", 0.48, q, None, "t", league="NBA")            # +4.2%
    ncaab = await sup._evaluate("novig", 0.48, q, None, "t", league="NCAAB")
    assert nba.action == "BET" and ncaab.action == "PASS" and "NCAAB minimum" in ncaab.reason


def test_maker_quotes_never_outlive_the_cutoff():
    from novig_v3 import NovigSigner, NovigV3Client, NovigV3OrderGateway
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    gw = NovigV3OrderGateway(NovigV3Client("https://x", NovigSigner("k", Ed25519PrivateKey.generate())),
                             tif="GTT", ttl_ms=300_000)
    gw.ttl_for = lambda oid: 90_000                                     # 90 s until the maker cutoff
    assert gw.order_body("o", 50, 1, "c")["ttl"] == 90_000
    gw.ttl_for = lambda oid: 500
    with pytest.raises(ValueError):
        gw.order_body("o", 50, 1, "c2")


async def test_a_failed_second_pair_leg_never_leaves_the_first_marked_hedged():
    from tests.test_arb_pairs import KEY, show
    from tests.test_kalshi_trading import K_BOS, kalshi_sup
    sup = kalshi_sup()

    async def down(*a, **k):
        raise RuntimeError("novig down")
    from novig_private import FillSlip
    await show(sup, upd("O-NYK", 0.51, volume=500))
    sup.order_gateway.place_limit = down                                 # the second leg's venue fails
    await show(sup, upd(K_BOS, 0.46, volume=400))                       # pair: Kalshi leg first, then Novig
    await sup.on_fill_slip(FillSlip(order_id="k1", status="FILLED", filled_volume=400, price_cents=46,
                                    venue="kalshi"))
    await asyncio.sleep(0.01)
    pos = sup.positions.get(KEY)
    assert pos is not None and not pos.hedged                            # the Kalshi leg stands alone: SEEN as naked
    assert sup.game_unhedged(KEY[:3]) > 0


async def test_kalshi_fees_count_against_the_canary_stake():
    from tests.test_kalshi_trading import K_NYK, kalshi_sup
    from tests.test_research import book_upd
    sup = kalshi_sup(max_stake=10.0)
    await sup.on_market_update(book_upd(K_NYK, [(0.47, 15), (0.48, 100)]), None)        # fair 0.5217
    total = sum(o["contracts"] * o["price_cents"] / 100 for o in sup.kalshi_gateway.placed)
    from execution import kalshi_fee_per_contract
    fees = sum(o["contracts"] * kalshi_fee_per_contract(o["price_cents"]) for o in sup.kalshi_gateway.placed)
    assert sup.kalshi_gateway.placed and total + fees <= 10.0 + 0.01
