import csv
import os
import tempfile
from datetime import date, datetime

import results
import test_kis
import trading

CFG = dict(commission=0.001, sell_tax=0.002, budget_krw=140_000, fast=20, slow=60, live_tickers=["005930", "000660"])
NOW = datetime(2026, 10, 6, 9, 40)


def rows(d, name):
    with open(os.path.join(d, "results", name), newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def in_tmp(fn):
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            fn(d)
        finally:
            os.chdir(old)


def order(side="buy", qty=4, value=40_000.0, day="20261006"):
    return {"ticker": "005930", "side": side, "qty": qty, "day": day, "status": "filled", "booked_qty": qty, "booked_value": value}


def test_bom_once_and_daily_never_twice():
    def go(d):
        r = results.day_row("2026-10-06", equity=139_000, cash=100_000, prev_equity=140_000, budget=140_000, peak=140_000)
        assert results.log_daily(r) and not results.log_daily(r)
        assert results.log_daily({**r, "date": "2026-10-07"})
        raw = open(results.DAILY, "rb").read()
        assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\xef\xbb\xbf") == 1
        a, b = rows(d, "daily_results.csv")
        assert (a["daily_pnl"], a["daily_pnl_pct"], a["total_pnl_vs_budget"], a["positions_value"], a["drawdown_pct"]) == \
            ("-1000", "-0.71", "-1000", "39000", "0.71")
    in_tmp(go)


def test_trades_idempotent_partial_and_excel_text():
    def go(d):
        o = {"1": order(qty=4, value=40_000.0)}
        o["1"].update(booked_qty=2, booked_value=20_000.0, status="partial")
        assert results.log_trades(o, CFG, {"005930": "삼성전자"}, NOW) == 1
        assert results.log_trades(o, CFG, {}, NOW) == 0
        o["1"].update(booked_qty=4, booked_value=42_000.0, status="filled")  # second half cost 22,000
        assert results.log_trades(o, CFG, {}, NOW) == 1 and results.log_trades(o, CFG, {}, NOW) == 0
        a, b = rows(d, "trades.csv")
        assert a["ticker"] == '="005930"' and a["order_no"] == '="1"' and a["name"] == "삼성전자" and a["source"] == "trading"
        assert (a["qty"], a["avg_price"], a["commission"], a["tax"], a["time"]) == ("2", "10000.0", "20.0", "0", "09:40:00")
        assert (b["qty"], b["avg_price"], b["value"]) == ("2", "11000.0", "22000.0")
        s = {"2": order("sell", qty=1, value=11_000.0, day="20261005")}  # late booking of an earlier day's order
        results.log_trades(s, CFG, {}, NOW)
        assert rows(d, "trades.csv")[-1]["tax"] == "22.0" and rows(d, "trades.csv")[-1]["time"] == ""
        assert rows(d, "trades.csv")[-1]["date"] == "2026-10-05"
    in_tmp(go)


def test_backfill_marks_order_test_and_is_repeatable():
    def go(d):
        st = {"bot_cash": 109_895.49, "equity": 139_995, "peak": 140_000, "orders": {"7": {**order(), "source": "order_test"}}}
        results.backfill(st, CFG, "20261006", {"005930": "KEPCO"}, NOW)
        results.backfill(st, CFG, "20261006", {"005930": "KEPCO"}, NOW)
        (t,), (r,) = rows(d, "trades.csv"), rows(d, "daily_results.csv")
        assert t["time"] == "" and t["source"] == "order_test" and r["orders_sent"] == "0" and "order_test" in r["notes"]
    in_tmp(go)


def test_signal_lines():
    cfg = {**CFG, "live_tickers": ["A", "B", "C"]}
    out = trading.signal_lines({"A": (32_100.4, 33_400.0), "B": (101.0, 100.0)}, cfg, {"A": "KEPCO"})
    assert out == ["KEPCO: SMA20 32,100 < SMA60 33,400 \u2192 flat", "B: SMA20 101 > SMA60 100 \u2192 long",
                   "C: no fresh signal (stale or short data)"]


def test_real_run_logs_once_dry_run_logs_nothing():
    b, alerts, d = test_kis.run_main(["--offline"], date(2026, 10, 6))
    (r,) = rows(d, "daily_results.csv")
    assert (r["date"], r["orders_sent"], r["orders_filled"], r["kill_switch_active"], r["reconcile_ok"]) == \
        ("2026-10-06", str(len(test_kis.LIVE)), str(len(test_kis.LIVE)), "False", "True")
    assert len(rows(d, "trades.csv")) == len(test_kis.LIVE) and {t["source"] for t in rows(d, "trades.csv")} == {"trading"}
    assert "SMA20" in alerts[-1] and "\u2192 long" in alerts[-1]

    b, alerts, d = test_kis.run_main(["--offline", "--dry-run"], date(2026, 10, 6))
    assert not os.path.exists(os.path.join(d, "results"))
    assert sum("SMA20" in line for line in alerts[-1].splitlines()) == len(test_kis.LIVE)  # one line per live ticker


def test_skipped_days_get_a_row_with_reason():
    st = {"bot_cash": 100_000, "equity": 139_000, "peak": 140_000, "positions": {}}
    _, _, d = test_kis.run_main(["--offline"], date(2026, 10, 9), state=st)  # Hangul Day
    (r,) = rows(d, "daily_results.csv")
    assert "not a trading day" in r["notes"] and r["sleeve_equity"] == "139000" and r["daily_pnl"] == ""
    _, _, d = test_kis.run_main(["--offline"], date(2026, 10, 6), state=st, at=(9, 45))
    (r,) = rows(d, "daily_results.csv")
    assert "outside trade window" in r["notes"] and r["orders_sent"] == "0"
    assert not os.path.exists(os.path.join(d, "results", "trades.csv"))


def test_kill_switch_row():
    st = {"bot_cash": 40_000, "equity": 140_000, "peak": 140_000, "positions": {"015760": 10}}
    held = {"015760": {"qty": 10, "avg_price": 10_000.0, "name": "KEPCO"}}
    _, _, d = test_kis.run_main(["--offline"], date(2026, 10, 6), test_kis.FakeBroker(price=9_400, positions=held, equity=9_999_000), st)
    (r,) = rows(d, "daily_results.csv")
    assert r["kill_switch_active"] == "True" and "previous run" in r["notes"] and r["orders_sent"] == "0"


def test_locked_csv_does_not_break_the_run():
    alerts = []
    trading.send_alert = alerts.append
    trading._log_results(lambda: (_ for _ in ()).throw(PermissionError("locked")))
    assert alerts and "Result log failed" in alerts[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
