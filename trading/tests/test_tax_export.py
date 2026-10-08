import json

from tax_export import totals, wager_rows


def _line(**kw):
    return json.dumps({"event": "SETTLE", **kw})


def test_every_wager_is_its_own_row_and_wins_and_losses_are_never_netted():
    lines = [
        _line(ts=1_790_000_000, venue="novig", settlement_id="s1", outcome_id="o1", game_id="g", market_type="moneyline",
              side="home", stake_usd=48.0, payout_usd=100.0, net_profit_usd=52.0, result="WIN"),
        _line(ts=1_790_000_100, venue="kalshi", kind="PARLAY", position="KXMVE-1", stake_usd=80.0, payout_usd=0.0,
              net_profit_usd=-80.0, result="yes"),
        _line(ts=1_790_000_200, venue="novig", kind="PARLAY", position="rfq9", stake_usd=30.0, net_profit_usd=12.5),
        _line(ts=1_790_000_100, venue="kalshi", kind="PARLAY", position="KXMVE-1", net_profit_usd=-80.0),  # replay
        _line(ts=1_790_000_300, venue="novig", settlement_id="s2", net_profit_usd=None),
        json.dumps({"event": "FILL"}), "not json",
    ]
    rows = wager_rows(lines, 2026)
    assert len(rows) == 4
    rfq = next(r for r in rows if r["id"] == "rfq9")
    assert rfq["payout_usd"] == 42.5 and rfq["win_or_loss"] == "WIN"           # payout derived from stake + net
    assert next(r for r in rows if r["id"] == "s2")["win_or_loss"].startswith("UNKNOWN")
    t = totals(rows)
    assert t == dict(wagers=4, gross_wins=64.5, gross_losses=80.0, net=-15.5, unknown=1)
    assert wager_rows(lines, 2025) == []


def test_parlay_settlements_carry_stake_and_payout_into_the_ledger(tmp_path):
    from tests.test_live_execution import live_sup
    sup = live_sup(ledger_path=tmp_path / "l.jsonl")
    sup._on_parlay_settled("novig", -40.0, "rfq1", stake_usd=40.0, payout_usd=0.0, result="won")
    row = [json.loads(x) for x in (tmp_path / "l.jsonl").read_text().splitlines() if '"SETTLE"' in x][-1]
    assert row["stake_usd"] == 40.0 and row["payout_usd"] == 0.0 and row["kind"] == "PARLAY"
    assert wager_rows((tmp_path / "l.jsonl").read_text().splitlines())[0]["win_or_loss"] == "LOSS"
