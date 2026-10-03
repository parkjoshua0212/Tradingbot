import numpy as np
import pandas as pd

import backtest
import data

CFG = dict(initial_cash=1_000_000, commission=0.001, sell_tax=0.002, slippage=0.0, fast=2, slow=3)


def bars(opens, closes):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"open": opens, "close": closes}, index=idx)


def test_no_lookahead_and_costs():
    # flat, then a jump on the last bar: strategy must not benefit from it
    closes = [100, 100, 100, 100, 100, 200]
    r = backtest.run(bars(closes, closes), CFG)
    assert r["buys"] == [] and r["equity"].iloc[-1] == 1_000_000

    # up-trend: signal at bar 2 close, fill at bar 3 open (price 110), costs applied
    closes = [100, 101, 102, 110, 120, 130]
    r = backtest.run(bars(closes, closes), CFG)
    n = int(1_000_000 // (110 * 1.001))
    assert r["buys"] == [3]
    assert abs(r["equity"].iloc[-1] - ((1_000_000 - n * 110 * 1.001) + n * 130)) < 1e-6


def test_sell_pays_tax():
    closes = [100, 101, 102, 110, 120, 90, 80, 70]
    r = backtest.run(bars(closes, closes), CFG)
    assert r["equity"].iloc[-1] < 1_000_000  # bought high, sold low, paid all costs


def test_signal_truncation_invariant():
    """signal at t from data[:t+1] == signal at t from the full series, for every t."""
    closes = pd.Series(np.random.default_rng(0).normal(0, 1, 300).cumsum() + 100)
    full = backtest.signals(closes, 5, 20)
    for t in range(len(closes)):
        trunc = backtest.signals(closes.iloc[: t + 1], 5, 20).iloc[-1]
        assert (np.isnan(trunc) and np.isnan(full.iloc[t])) or trunc == full.iloc[t], t


def test_upsert_idempotent():
    con = data.connect(":memory:")
    rows = [("005930", "2024-01-02", 1, 2, 0.5, 1.5, 10), ("005930", "2024-01-03", 1, 2, 0.5, 1.6, 11)]
    data.upsert(con, rows)
    data.upsert(con, rows)
    assert con.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 2
    data.upsert(con, [("005930", "2024-01-03", 1, 2, 0.5, 9.9, 11)])  # revision overwrites, no dup
    assert con.execute("SELECT COUNT(*), MAX(close) FROM ohlcv").fetchone() == (2, 9.9)


def test_bh_aligned_with_first_fill():
    # steady up-trend: strategy buys at the first possible bar and never sells,
    # so B&H starting at that same bar must produce identical results.
    closes = list(np.linspace(100, 200, 40))
    df = bars(closes, closes)
    res = backtest.run(df, CFG)
    assert res["buys"][0] == CFG["slow"]
    ev = backtest.evaluate(df, res, CFG, CFG["slow"], len(df))
    for k in ("ret", "maxdd", "sharpe"):
        assert abs(ev["strategy"][k] - ev["bh"][k]) < 1e-9, k


def test_portfolio_uses_live_sizing():
    """Two identical tickers; zero costs, 1% buffer. Hand-checked fill."""
    cfg = {**CFG, "initial_cash": 1_000, "cash_buffer": 0.01, "commission": 0.0}
    px = [100.0, 99.0, 98.0, 100.0, 110.0, 120.0, 130.0, 130.0]
    dfs = {"A": bars(px, px), "B": bars(px, px)}
    res = backtest.run_portfolio(dfs, cfg)
    # signal turns 1 at bar 4 close; fills at bar 5 open=120: slice 500, buffered cost 121.2 -> 4 shares each
    assert res["shares"] == {"A": 4, "B": 4} and res["buys"] == 2
    eq = res["equity"]
    assert abs(eq.iloc[-1] - (1_000 - 2 * 4 * 120 + 2 * 4 * 130)) < 1e-9  # cash 40 + 8 shares * 130
    assert (eq > 0).all()
    bh = backtest.run_portfolio(dfs, cfg, always_long=True)
    assert bh["buys"] == 2 and bh["shares"] == res["shares"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
