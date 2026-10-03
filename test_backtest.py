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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
