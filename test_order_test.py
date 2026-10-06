import json
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import order_test
import trading

CFG = dict(commission=0.00015, sell_tax=0.002, cash_buffer=0.01, budget_krw=140_000, live_tickers=["A", "B"])
NOW = datetime(2026, 10, 6, 9, 40, tzinfo=ZoneInfo("Asia/Seoul"))


class Fake:
    def get_price(self, t): return {"A": 5000, "B": 3000}[t]
    def get_balance(self): return {"positions": {}}
    def place_order(self, t, side, qty, kind): self.order = (t, side, qty); return "0001"
    def get_order_status(self, oid, day): return {"filled": 1, "avg_price": 3010.0, "open": False, "qty": 1}


def test_buys_cheapest_and_books_sleeve_without_last_run():
    old, trading.time.sleep = os.getcwd(), lambda s: None
    trading.send_alert = lambda text: None
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            json.dump({"bot_cash": 140000, "equity": 140000, "positions": {}, "orders": {}, "peak": 140000}, open("state.json", "w"))
            c = Fake()
            out = order_test.buy_one(c, CFG, NOW)
            assert c.order == ("B", "buy", 1) and "order 0001" in out and "₩3,010" in out
            st = json.load(open("state.json"))
            assert st["positions"] == {"B": 1} and abs(st["bot_cash"] - (140000 - 3010 * 1.00015)) < 0.01
            assert st["orders"]["0001"]["status"] == "filled" and st["equity"] == round(st["bot_cash"] + 3000)
            assert not os.path.exists("last_run.txt")
        finally:
            os.chdir(old)


def test_guards():
    for at, ok in [((8, 59), False), ((9, 0), True), ((15, 20), True), ((15, 21), False)]:
        try:
            order_test.check_window(NOW.replace(hour=at[0], minute=at[1]), ["2026-10-09"])
            assert ok, at
        except SystemExit:
            assert not ok, at
    try:
        order_test.check_window(NOW.replace(day=10), ["2026-10-09"])  # Saturday
        raise AssertionError
    except SystemExit:
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
