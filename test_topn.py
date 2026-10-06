import numpy as np
import pandas as pd

import strategy_topn as st

CFG = dict(commission=0.001, sell_tax=0.002, slippage=0.001, cash_buffer=0.01, initial_cash=100_000_000, n=2,
           sma_fast=3, sma_slow=8, mom_lookback=20, mom_skip=2, min_avg_value_krw=1_000, liq_window=5)


def panels(drifts: dict, bars: int = 200, seed: int = 0, drop: tuple = (), sigma: float = 0.01) -> dict:
    """Random walks with per-ticker drift. `drop` removes calendar days (holidays) from the index."""
    idx = pd.bdate_range("2024-01-01", periods=bars).drop(pd.to_datetime(list(drop)))
    rng = np.random.default_rng(seed)
    cl = pd.DataFrame({t: 100 * np.exp(np.cumsum(rng.normal(m, sigma, len(idx)))) for t, m in drifts.items()}, index=idx)
    op = cl.shift(1).fillna(cl.iloc[0]) * 1.001
    return dict(open=op, close=cl, volume=pd.DataFrame(1_000_000, index=idx, columns=cl.columns))


def test_features_truncation_invariant():
    """score at t from data[:t+1] == score at t from the full panel, for every t (no look-ahead)."""
    p = panels({"A": 0.002, "B": 0.0, "C": -0.001})
    full = st.features(p["close"], p["volume"], CFG)
    for t in range(len(full)):
        tr = st.features(p["close"].iloc[: t + 1], p["volume"].iloc[: t + 1], CFG).iloc[-1]
        pd.testing.assert_series_equal(tr, full.iloc[t], check_names=False)


def test_run_truncation_invariant():
    """Equity up to bar t is identical whether or not later bars exist."""
    p = panels({"A": 0.003, "B": 0.002, "C": 0.001, "D": 0.0}, bars=120)
    snaps = {"2024-01-01": set("ABCD")}
    full = st.run(p, snaps, CFG)["equity"]
    cut = 90
    part = st.run({k: v.iloc[:cut] for k, v in p.items()}, snaps, CFG)["equity"]
    np.testing.assert_allclose(part.values[:-1], full.values[: cut - 1])  # last bar of part: week may be cut mid-way


def test_members_are_point_in_time():
    p = panels({"X": 0.004, "A": 0.002, "B": 0.001, "Z": 0.005}, bars=200)  # Z is the best performer but never a member
    cal = p["close"].index
    removed = cal[100].strftime("%Y-%m-%d")
    snaps = {cal[0].strftime("%Y-%m-%d"): {"X", "A", "B"}, removed: {"A", "B"}}
    res = st.run(p, snaps, CFG)
    buys = [(f["t"], f["i"]) for f in res["fills"] if f["side"] == "buy"]
    assert any(t == "X" for t, _ in buys), "X should be picked while it is a member"
    assert all(i - 1 < 100 for t, i in buys if t == "X"), "X bought on a decision made after its removal"
    assert all(t != "Z" for t, _ in buys), "Z was never in the index"
    assert "X" not in res["shares"], "removed X must be sold after the next weekly decision"
    assert st.members_on(snaps, cal[99]) == {"X", "A", "B"} and st.members_on(snaps, cal[100]) == {"A", "B"}
    assert st.members_on(snaps, pd.Timestamp("2023-12-01")) == set()


def test_rebalance_only_on_last_trading_day_of_week():
    holiday = "2024-03-01"  # Friday: Thursday 02-29 becomes the week's last trading day
    p = panels({"A": 0.003, "B": 0.002, "C": 0.001}, drop=(holiday,))
    cal = p["close"].index
    reb = [d for i, d in enumerate(cal) if st.is_rebalance(cal, i)]
    assert pd.Timestamp("2024-02-29") in reb and pd.Timestamp("2024-02-28") not in reb
    assert all(d.weekday() in (3, 4) for d in reb) and not st.is_rebalance(cal, len(cal) - 1)
    res = st.run(p, {"2024-01-01": set("ABC")}, CFG)
    assert res["fills"]
    for f in res["fills"]:  # fill bar = the bar after a weekly decision close
        assert st.is_rebalance(cal, f["i"] - 1), cal[f["i"]]
        assert cal[f["i"]].isocalendar()[:2] != cal[f["i"] - 1].isocalendar()[:2]


def test_fill_next_open_with_costs_and_stayers_untouched():
    p = panels({"A": 0.004, "B": 0.003, "C": -0.002}, bars=150, sigma=0.0)  # noise-free: leaders stay eligible
    res = st.run(p, {"2024-01-01": set("ABC")}, CFG)
    first = res["fills"][0]
    o = p["open"]
    assert first["side"] == "buy" and first["notional"] == first["qty"] * o.iloc[first["i"]][first["t"]]
    # slice = equity / n, sized at open*(1+slip)*(1+buffer): shares never exceed that
    assert first["qty"] <= CFG["initial_cash"] / CFG["n"] / (o.iloc[first["i"]][first["t"]] * 1.001 * 1.01)
    # steady leaders never churn: a stayer is bought once and never sold or re-bought
    for t in ("A", "B"):
        assert sum(f["t"] == t and f["side"] == "buy" for f in res["fills"]) == 1, t
    assert res["nhold"].max() == CFG["n"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
