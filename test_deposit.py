import json
import os
import tomllib
from datetime import date

import deposit
import results
import test_kis
import test_results
import trading

BUDGET = 3_000_000
# 14 shares at 10,000 and no cash = 140,000 sleeve (FakeBroker fills everything at 10,000)
BEFORE = {"bot_cash": 0, "equity": 140_000, "peak": 140_000, "positions": {"015760": 14}, "orders": {}}
HELD = {"015760": {"qty": 14, "avg_price": 10_000.0, "name": "KEPCO"}}


def run(state, price):
    return test_kis.run_main(["--offline"], date(2026, 10, 6), test_kis.FakeBroker(price=price, positions=HELD, equity=9_999_000), state)


def test_apply_deposit_shifts_cash_equity_and_peak_and_refuses_repeat():
    s = deposit.apply_deposit(BEFORE, 2_860_000, "2026-10-06")
    assert (s["bot_cash"], s["equity"], s["peak"]) == (2_860_000, 3_000_000, 3_000_000)
    assert s["deposits"] == [{"date": "2026-10-06", "amount": 2_860_000}] and s["positions"] == BEFORE["positions"]
    assert BEFORE["bot_cash"] == 0 and "deposits" not in BEFORE  # input untouched
    try:
        deposit.apply_deposit(s, 2_860_000, "2026-10-06")
        raise AssertionError
    except ValueError:
        pass


def test_deposit_alone_does_not_trigger_the_kill_switch():
    s = deposit.apply_deposit(BEFORE, 2_860_000, "2026-10-06")
    b, alerts, d = run(s, price=10_000)  # prices unchanged: equity ~3,000,000 (minus fees on today's buys) vs adjusted baseline
    assert not os.path.exists(os.path.join(d, "KILL")) and not any("Trading stopped" in a for a in alerts)
    (r,) = test_results.rows(d, "daily_results.csv")
    assert r["kill_switch_active"] == "False" and abs(float(r["daily_pnl"])) < 1_000 and float(r["drawdown_pct"]) < 0.05
    st = json.load(open(os.path.join(d, "state.json")))
    assert st["deposits"] == s["deposits"]  # a normal run must not drop the deposit record


def test_deposit_does_not_mask_a_real_loss():
    s = deposit.apply_deposit(BEFORE, 2_860_000, "2026-10-06")
    b, alerts, d = run(s, price=2_000)  # position 140,000 -> 28,000: sleeve 2,888,000 = -3.7% vs 3,000,000
    assert os.path.exists(os.path.join(d, "KILL")) and any("Trading stopped" in a for a in alerts)
    # the unadjusted state (cash added, baselines left at 140,000) reads as a huge gain and never fires:
    naive = {**BEFORE, "bot_cash": 2_860_000}
    b, alerts, d = run(naive, price=2_000)
    assert not os.path.exists(os.path.join(d, "KILL"))


def test_pnl_after_deposit_is_against_the_new_base():
    s = deposit.apply_deposit(BEFORE, 2_860_000, "2026-10-06")
    b, alerts, d = run(s, price=10_000)
    (r,) = test_results.rows(d, "daily_results.csv")
    eq = int(r["sleeve_equity"])
    assert 2_995_000 < eq < 3_000_000 and r["kill_switch_active"] == "False"
    assert int(r["total_pnl_vs_budget"]) == eq - BUDGET and int(r["daily_pnl"]) == eq - BUDGET  # base 3,000,000, not 140,000
    assert f"{eq - BUDGET:+,}" in alerts[-1] and "vs budget ₩3,000,000" in alerts[-1]
    assert r["peak_equity"] == "3000000"


def test_deposit_row_sits_beside_the_day_row_once():
    def go(dr):
        day_row = results.day_row("2026-10-06", equity=139_995, cash=109_895, prev_equity=None, budget=BUDGET, peak=140_000)
        results.log_daily(day_row)
        dep = results.day_row("2026-10-06", equity=2_999_995, cash=2_969_895, prev_equity=2_999_995, budget=BUDGET, peak=3_000_000,
                              notes="deposit +2,860,000 (capital, not P&L)")
        assert results.log_daily(dep) and not results.log_daily(dep)
        day, dp = test_results.rows(dr, "daily_results.csv")
        assert dp["daily_pnl"] == "0" and dp["total_pnl_vs_budget"] == "-5" and "not P&L" in dp["notes"]
        assert not results.log_daily(day_row)  # the normal row is still once-per-day
    test_results.in_tmp(go)


def test_names_table_used_for_lines_and_trades():
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    assert cfg["names"] == {"015760": "KEPCO / 한국전력", "035720": "Kakao / 카카오", "251270": "Netmarble / 넷마블"}
    assert set(cfg["names"]) == set(cfg["live_tickers"]) and cfg["budget_krw"] == BUDGET
    names = trading._names({"015760": {"name": "한국전력공사", "qty": 1}}, cfg)
    assert names["015760"] == "KEPCO / 한국전력"  # the table wins over the broker's name
    line = trading.signal_lines({"015760": (1.0, 2.0)}, {**cfg, "live_tickers": ["015760"]}, names)[0]
    assert line.startswith("KEPCO / 한국전력: SMA20")
    b, alerts, d = test_kis.run_main(["--offline"], date(2026, 10, 6))
    assert {t["name"] for t in test_results.rows(d, "trades.csv")} == set(cfg["names"].values())
    assert "Kakao / 카카오: SMA20" in alerts[-1]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
