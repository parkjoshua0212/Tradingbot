"""Mock-HTTP tests only; no network. Run: python test_kis.py"""
import json
import os
import tempfile

import kis_client
import trading
from kis_client import KISClient, KISError, RateLimiter


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body if body is not None else {}
        self.content, self.text = json.dumps(self._b).encode(), json.dumps(self._b)

    def json(self):
        return self._b


class FakeSession:
    """Replays queued responses; records calls."""

    def __init__(self, *resps):
        self.resps, self.calls = list(resps), []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.resps.pop(0)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def request(self, method, url, **kw):
        return self._next(method, url, **kw)


TOK = {"access_token": "T1", "expires_in": 86400}
OK = {"rt_cd": "0", "output": {"stck_prpr": "70000"}}


def client(session, now=1000.0, tf=None, **kw):
    t = [now]
    c = KISClient("k", "s", "123", "01", session=session, token_file=tf or os.path.join(tempfile.mkdtemp(), "t.json"),
                  clock=lambda: t[0], sleep=lambda s: None, limiter=RateLimiter(0, sleep=lambda s: None), **kw)
    c._t = t
    return c


def test_mock_url_guard():
    try:
        KISClient("k", "s", "1", "01", base_url="https://openapi.koreainvestment.com:9443")
    except ValueError:
        return
    raise AssertionError("live URL accepted")


def test_token_cache_reuse_and_expiry():
    tf = os.path.join(tempfile.mkdtemp(), "t.json")
    s = FakeSession(Resp(200, TOK), Resp(200, OK), Resp(200, OK))
    c = client(s, tf=tf)
    c.get_price("005930")
    c.get_price("005930")                      # memory reuse
    assert [x[0] for x in s.calls].count("POST") == 1
    s2 = FakeSession(Resp(200, OK))
    c2 = client(s2, tf=tf)                     # new process, same file: reuse from disk
    c2.get_price("005930")
    assert s2.calls[0][2]["headers"]["authorization"] == "Bearer T1"
    s3 = FakeSession(Resp(200, {"access_token": "T2", "expires_in": 86400}), Resp(200, OK))
    c3 = client(s3, now=1000 + 86400 - 100, tf=tf)  # inside the 10 min margin: reissue
    c3.get_price("005930")
    assert s3.calls[0][1].endswith("/oauth2/tokenP") and c3._token["token"] == "T2"


def test_rate_limiter():
    t, slept = [0.0], []
    rl = RateLimiter(0.5, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    rl.wait()
    rl.wait()
    assert slept == [0.5]
    t[0] += 10
    rl.wait()
    assert slept == [0.5]


def test_retry_get_but_never_order():
    s = FakeSession(Resp(200, TOK), Resp(500, {}), Resp(200, OK))
    assert client(s).get_price("005930") == 70000
    s = FakeSession(Resp(200, TOK), Resp(500, {"msg_cd": "EGW00201", "msg1": "rate"}), Resp(200, {"rt_cd": "0", "output": {"ODNO": "1"}}))
    c = client(s)
    try:
        c.place_order("005930", "buy", 1)
    except KISError:
        pass
    else:
        raise AssertionError("expected KISError")
    assert [x[0] for x in s.calls].count("POST") == 2  # token POST + exactly one order POST... order uses request()
    assert sum(1 for x in s.calls if "order-cash" in x[1]) == 1


def test_order_validation():
    c = client(FakeSession())
    for args in [("005930", "buy", 0), ("005930", "buy", 1.5), ("005930", "hold", 1)]:
        try:
            c.place_order(*args)
        except ValueError:
            continue
        raise AssertionError(args)


CFG = {"cash_buffer": 0.0, "max_daily_loss_pct": 3.0, "max_drawdown": 0.15}


def test_kill_switch():
    kf = os.path.join(tempfile.mkdtemp(), "KILL")
    k = lambda eq, last, peak: trading.kill_reason(eq, last, peak, CFG, kf)
    assert k(100, 100, 100) is None
    assert k(98, 100, 100) is None
    assert "previous run" in k(96, 100, 100)
    assert k(100, None, None) is None  # first run: no baseline
    open(kf, "w").close()
    assert "KILL" in k(100, 100, 100)


def test_drawdown_vs_peak():
    kf = os.path.join(tempfile.mkdtemp(), "KILL")
    assert trading.kill_reason(86, 86, 100, CFG, kf) is None  # 14% down, within the 15% limit
    assert "peak" in trading.kill_reason(84, 84, 100, CFG, kf)
    assert "peak" in trading.kill_reason(84, None, 100, CFG, kf)


def test_reconcile_diff():
    live = ["A", "B", "C"]
    bal = {"orderable": 1000, "positions": {"A": {"qty": 5}, "C": {"qty": 1}, "X": {"qty": 9}}}  # X: not a live ticker
    assert trading.reconcile({"cash": 1500, "positions": {"A": 5, "C": 1}}, bal, live) == []  # cash within tolerance
    d = trading.reconcile({"cash": 9000, "positions": {"A": 4, "B": 2}}, bal, live)
    assert len(d) == 4 and any("A:" in x for x in d) and any("B:" in x for x in d) and any("cash" in x for x in d)
    assert any("C:" in x and "did not create" in x for x in d) and not any("X" in x for x in d)


def test_apply_fill_costs():
    cfg = {"commission": 0.001, "sell_tax": 0.002}
    s = {"cash": 100_000, "positions": {}}
    trading.apply_fill(s, cfg, "A", "buy", 3, 10_000)
    assert s["positions"] == {"A": 3} and abs(s["cash"] - (100_000 - 30_030)) < 1e-9
    trading.apply_fill(s, cfg, "A", "sell", 3, 11_000)
    assert s["positions"] == {} and abs(s["cash"] - (69_970 + 33_000 * (1 - 0.003))) < 1e-9


def test_plan_orders_integer_cash_capped_and_zero_skip():
    bal = {"orderable": 150, "equity": 1000, "positions": {"B": {"qty": 7}}}
    o, sk = trading.plan_orders({"A": 1, "B": 0, "C": 1}, bal, {"A": 40, "C": 90}, CFG, 2)
    assert ("B", "sell", 7) in o and ("A", "buy", 3) in o  # slice 500, cash 150 -> 3 shares at 40
    assert sk == ["C"]                                       # 30 cash left < 90: 0 shares, reported
    o, sk = trading.plan_orders({"A": 1}, {"orderable": 1000, "equity": 1000, "positions": {}}, {"A": 100}, {**CFG, "cash_buffer": 0.01}, 1)
    assert o == [("A", "buy", 9)]                            # 1% buffer: 100*1.01 -> 9 shares, not 10


def test_holidays_and_weekends():
    import tomllib
    from datetime import date
    h = tomllib.load(open("config.toml", "rb"))["holidays"]
    assert not trading.is_trading_day(date(2026, 10, 5), h)   # substitute holiday for Gaecheonjeol (Sat 10/3)
    assert not trading.is_trading_day(date(2026, 10, 3), h)   # Saturday
    assert not trading.is_trading_day(date(2026, 9, 25), h)   # Chuseok
    assert not trading.is_trading_day(date(2027, 2, 9), h)    # Seollal substitute
    assert trading.is_trading_day(date(2026, 10, 6), h)
    assert trading.is_trading_day(date(2027, 6, 7), h)
    try:
        trading.is_trading_day(date(2028, 1, 4), h)
    except ValueError:
        return
    raise AssertionError("uncovered year accepted")


class LateBroker:
    """Order statuses are whatever the test sets in .fill[oid] = (cumulative filled, avg price)."""
    def __init__(self):
        self.orders, self.fill = [], {}

    def place_order(self, t, side, qty, order_type="market"):
        self.orders.append(qty)
        return str(len(self.orders))

    def get_order_status(self, oid, day):
        qty, (filled, avg) = self.orders[int(oid) - 1], self.fill.get(oid, (0, 0.0))
        return {"qty": qty, "filled": filled, "avg_price": avg, "open": filled < qty}


def _submit(b, qty=4):
    trading.time.sleep, trading.send_alert = lambda s: None, lambda m: None
    sleeve = {"cash": 100_000, "positions": {}, "orders": {}}
    trading.execute(b, [("A", "buy", qty)], {}, "20261006", False, sleeve, COST, lambda: None)
    return sleeve


COST = {"commission": 0.001, "sell_tax": 0.002}


def test_late_fill_booked_next_run():
    b = LateBroker()
    s = _submit(b)
    assert s["positions"] == {} and s["cash"] == 100_000 and s["orders"]["1"]["status"] == "pending"  # nothing filled at poll time
    b.fill["1"] = (4, 10_000.0)
    trading.sync_orders(b, s, COST, "20261007")
    assert s["positions"] == {"A": 4} and abs(s["cash"] - (100_000 - 40_040)) < 1e-9 and s["orders"]["1"]["status"] == "filled"


def test_partial_fill_books_only_filled_qty_then_the_rest():
    b = LateBroker()
    s = _submit(b)
    b.fill["1"] = (2, 10_000.0)
    trading.sync_orders(b, s, COST, "20261006")  # same day: still working
    assert s["positions"] == {"A": 2} and s["orders"]["1"]["status"] == "partial"
    b.fill["1"] = (4, 10_500.0)  # cumulative: 42,000 total, so the second half cost 22,000
    trading.sync_orders(b, s, COST, "20261006")
    assert s["positions"] == {"A": 4} and abs(s["cash"] - (100_000 - 42_000 * 1.001)) < 1e-9
    b2 = LateBroker()
    s2 = _submit(b2)
    b2.fill["1"] = (2, 10_000.0)
    trading.sync_orders(b2, s2, COST, "20261007")  # next day, 2 of 4 filled: day order expired
    assert s2["positions"] == {"A": 2} and s2["orders"]["1"]["status"] == "cancelled"


def test_booking_is_idempotent():
    import copy
    b = LateBroker()
    s = _submit(b)
    b.fill["1"] = (3, 10_000.0)
    trading.sync_orders(b, s, COST, "20261006")
    once = copy.deepcopy(s)
    trading.sync_orders(b, s, COST, "20261006")
    trading.sync_orders(b, s, COST, "20261007")
    assert s == {**once, "orders": {"1": {**once["orders"]["1"], "status": "cancelled"}}}  # only the expiry flag moved
    assert s["positions"] == once["positions"] and s["cash"] == once["cash"]


class FakeBroker:
    def __init__(self, price=10_000, positions=None, equity=10_000_000):
        self.orders, self.price, self.positions, self.equity = [], price, positions or {}, equity

    def get_balance(self):
        return {"cash": 10_000_000, "orderable": 10_000_000, "equity": self.equity, "positions": self.positions}

    def get_price(self, t):
        return self.price

    def place_order(self, t, side, qty, order_type="market"):
        self.orders.append((t, side, qty))
        return str(len(self.orders))

    def get_order_status(self, oid, day):
        t, side, qty = self.orders[int(oid) - 1]
        return {"qty": qty, "filled": qty, "avg_price": 10_000.0, "open": False}


def run_main(argv, today, broker=None, state=None, at=(9, 10)):
    """Runs trading.main in a temp dir with a synthetic DB and a fake broker; returns (broker, alerts, dir)."""
    import shutil
    import sqlite3
    from datetime import date, datetime, time as dtime, timedelta
    import data
    d = tempfile.mkdtemp()
    shutil.copy("config.toml", d)
    old = os.getcwd()
    os.chdir(d)
    try:
        cfg = {**__import__("tomllib").load(open("config.toml", "rb")), "db_path": "t.db"}
        con = data.connect("t.db")
        rows = [(t, (today - timedelta(days=k)).isoformat(), 1, 1, 1, 100 + (200 - k) * 0.1, 1) for t in cfg["live_tickers"] for k in range(1, 200)]
        data.upsert(con, rows)
        con.close()
        b, alerts = broker or FakeBroker(), []
        if state:
            json.dump(state, open("state.json", "w"))
        trading.KISClient = lambda **kw: b
        trading.send_alert = alerts.append
        trading.time.sleep = lambda s: None
        text = open("config.toml").read().replace('db_path = "ohlcv.db"', 'db_path = "t.db"')
        open("config.toml", "w").write(text)
        trading.main(argv, datetime.combine(today, dtime(*at)))
        return b, alerts, d
    finally:
        os.chdir(old)


def test_dry_run_writes_nothing_and_orders_nothing():
    from datetime import date
    b, alerts, d = run_main(["--offline", "--dry-run"], date(2026, 10, 6))
    assert b.orders == []
    assert not os.path.exists(os.path.join(d, "last_run.txt")) and not os.path.exists(os.path.join(d, "state.json"))
    assert any("dry run" in a for a in alerts)


def test_outside_window_blocks_real_run_but_not_dry_run():
    from datetime import date
    for at in [(8, 59), (9, 31), (15, 0)]:
        b, alerts, d = run_main(["--offline"], date(2026, 10, 6), at=at)
        assert b.orders == [] and len(alerts) == 1 and "09:00" in alerts[0]
        assert not os.path.exists(os.path.join(d, "last_run.txt")) and not os.path.exists(os.path.join(d, "state.json"))
    for at in [(9, 0), (9, 30)]:  # edges are inside
        assert len(run_main(["--offline"], date(2026, 10, 6), at=at)[0].orders) == 3
    b, alerts, d = run_main(["--offline", "--dry-run"], date(2026, 10, 6), at=(15, 0))
    assert any("dry run" in a for a in alerts)


def test_real_run_orders_and_marks_run():
    from datetime import date
    b, alerts, d = run_main(["--offline"], date(2026, 10, 6))  # rising prices: all live tickers long
    assert len(b.orders) == 3 and all(o[1] == "buy" and isinstance(o[2], int) and o[2] > 0 for o in b.orders)
    assert open(os.path.join(d, "last_run.txt")).read() == "20261006"
    st = json.load(open(os.path.join(d, "state.json")))
    assert st["peak"] == 140_000 and st["equity"] > 139_900  # sleeve, not the 10M account
    assert set(st["positions"]) == {"015760", "035720", "251270"} and 0 < st["bot_cash"] < 140_000
    assert len(st["orders"]) == 3 and all(o["status"] == "filled" for o in st["orders"].values())
    assert any("bot money" in a and "vs budget" in a for a in alerts)


def test_kill_switch_uses_sleeve_not_account():
    """Sleeve 140,000 -> 134,000 (-4.3%) while the 10M account moves 0.01%: the kill switch must fire."""
    from datetime import date
    state = {"bot_cash": 40_000, "equity": 140_000, "peak": 140_000, "positions": {"015760": 10}}
    held = {"015760": {"qty": 10, "avg_price": 10_000.0, "name": "KEPCO"}}
    b, alerts, d = run_main(["--offline"], date(2026, 10, 6), FakeBroker(price=9_400, positions=held, equity=9_999_000), state)
    assert b.orders == [] and os.path.exists(os.path.join(d, "KILL"))
    assert any("Trading stopped" in a and "previous run" in a for a in alerts)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
