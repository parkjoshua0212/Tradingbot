import math

import pandas as pd


def signals(closes: pd.Series, fast: int, slow: int) -> pd.Series:
    """1 = long, 0 = flat, NaN = not enough history. Rolling means are causal:
    value at t depends only on closes[:t+1] (tested)."""
    f, s = closes.rolling(fast).mean(), closes.rolling(slow).mean()
    return (f > s).astype(float).where(s.notna())


def run(df: pd.DataFrame, cfg: dict) -> dict:
    """Event loop over daily bars. Signal at bar i's close fills at bar i+1's open."""
    comm, tax, slip = cfg["commission"], cfg["sell_tax"], cfg["slippage"]
    sig = signals(df["close"], cfg["fast"], cfg["slow"])
    cash, shares, pending, buys, equity = cfg["initial_cash"], 0, None, [], []
    for i in range(len(df)):
        px = df["open"].iloc[i]
        if pending == 1 and shares == 0:
            fill = px * (1 + slip)
            n = int(cash // (fill * (1 + comm)))
            if n:
                cash -= n * fill * (1 + comm)
                shares = n
                buys.append(i)
        elif pending == 0 and shares > 0:
            cash += shares * px * (1 - slip) * (1 - comm - tax)
            shares = 0
        equity.append(cash + shares * df["close"].iloc[i])
        pending = None if math.isnan(sig.iloc[i]) else int(sig.iloc[i])
    return {"equity": pd.Series(equity, index=df.index), "buys": buys}


def _stats(curve: pd.Series, base: float) -> dict:
    c = pd.concat([pd.Series([base]), curve.reset_index(drop=True)])
    r = c.pct_change().dropna()
    sharpe = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else float("nan")  # rf = 0
    return {"ret": c.iloc[-1] / base - 1, "maxdd": (c / c.cummax() - 1).min(), "sharpe": sharpe}


def evaluate(df: pd.DataFrame, res: dict, cfg: dict, lo: int, hi: int) -> dict | None:
    """Strategy vs buy-and-hold over bars [lo, hi). Both start from the same capital
    (strategy equity at the prior close); B&H buys at bar lo's open with the same costs.
    First possible strategy fill is bar `slow`, so use lo >= slow for an aligned comparison."""
    if hi - lo < 2:
        return None
    eq = res["equity"]
    base = eq.iloc[lo - 1] if lo > 0 else cfg["initial_cash"]
    strat = _stats(eq.iloc[lo:hi], base)
    strat["trades"] = sum(lo <= b < hi for b in res["buys"])
    fill = df["open"].iloc[lo] * (1 + cfg["slippage"])
    n = int(base // (fill * (1 + cfg["commission"])))
    left = base - n * fill * (1 + cfg["commission"])
    bh = _stats(left + n * df["close"].iloc[lo:hi], base)
    return {"strategy": strat, "bh": bh}
