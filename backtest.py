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


def plan_orders(targets: dict, balance: dict, prices: dict, cfg: dict, n_slices: int) -> tuple[list, list]:
    """Live sizing rules, shared by trading.py and run_portfolio so paper results are comparable.
    Returns ([(ticker, side, qty)], [tickers that rounded to 0 shares]). Sells (everything held) first.
    Buys: one cash pool (balance["orderable"]), equal slice of equity per ticker, sized at
    price * (1 + cash_buffer), integer shares; the pool shrinks by the buffered cost as buys are planned."""
    held = {t: p["qty"] for t, p in balance["positions"].items()}
    orders = [(t, "sell", held[t]) for t, want in targets.items() if want == 0 and held.get(t)]
    cash, slice_, skipped = balance["orderable"], balance["equity"] / n_slices, []
    for t, want in targets.items():
        if want == 1 and not held.get(t):
            cost = prices[t] * (1 + cfg["cash_buffer"])
            qty = int(min(slice_, cash) // cost)
            if qty > 0:
                orders.append((t, "buy", qty))
                cash -= qty * cost
            else:
                skipped.append(t)
    return orders, skipped


def run_portfolio(dfs: dict, cfg: dict, always_long: bool = False) -> dict:
    """One cash pool over the live universe using plan_orders. Signal at close i fills at open i+1,
    costs as in run(). always_long=True gives an equal-slice buy-and-hold baseline.
    ponytail: only dates present for every ticker are used; a ticker halted on a day drops that day for all."""
    comm, tax, slip = cfg["commission"], cfg["sell_tax"], cfg["slippage"]
    idx = None
    for df in dfs.values():
        idx = df.index if idx is None else idx.intersection(df.index)
    sig = {t: signals(df["close"], cfg["fast"], cfg["slow"]).reindex(idx) for t, df in dfs.items()}
    cash, shares, pending, equity, buys, skipped = cfg["initial_cash"], {t: 0 for t in dfs}, None, [], 0, []
    for i, d in enumerate(idx):
        opens = {t: dfs[t]["open"].loc[d] for t in dfs}
        if pending is not None:
            bal = {"orderable": cash, "equity": cash + sum(shares[t] * opens[t] for t in dfs),
                   "positions": {t: {"qty": n} for t, n in shares.items() if n}}
            fills = {t: opens[t] * (1 + slip) for t in dfs}
            orders, sk = plan_orders(pending, bal, fills, cfg, len(dfs))
            skipped += [(d, t) for t in sk]
            for t, side, qty in orders:
                if side == "sell":
                    cash += qty * opens[t] * (1 - slip) * (1 - comm - tax)
                    shares[t] = 0
                else:
                    cash -= qty * fills[t] * (1 + comm)
                    shares[t] = qty
                    buys += 1
        equity.append(cash + sum(shares[t] * dfs[t]["close"].loc[d] for t in dfs))
        s = {t: sig[t].iloc[i] for t in dfs}
        if always_long:
            s = {t: (v if v != v else 1.0) for t, v in s.items()}  # same start as the strategy: first bar all signals exist
        pending = None if any(v != v for v in s.values()) else {t: int(v) for t, v in s.items()}
    return {"equity": pd.Series(equity, index=idx), "buys": buys, "skipped": skipped, "shares": shares}
