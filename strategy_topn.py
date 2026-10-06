"""Top-N momentum on point-in-time KOSPI 200 members. Backtest only; never imported by trading.py.
`python strategy_topn.py` backfills (resumable), runs once, prints the report. `--offline` skips the fetch."""
import sys
import time
import tomllib
from datetime import date, timedelta

import pandas as pd

import backtest
import data

SNAP_SCHEMA = """CREATE TABLE IF NOT EXISTS constituents (snap TEXT NOT NULL, ticker TEXT NOT NULL, PRIMARY KEY (snap, ticker));
CREATE TABLE IF NOT EXISTS fetched (ticker TEXT PRIMARY KEY, through TEXT NOT NULL, bars INTEGER NOT NULL)"""


def load_cfg() -> dict:
    with open("config.toml", "rb") as f:
        live = tomllib.load(f)
    with open("topn.toml", "rb") as f:
        cfg = tomllib.load(f)
    cfg.update({k: live[k] for k in ("commission", "sell_tax", "slippage", "cash_buffer")})
    cfg["initial_cash"] = cfg["budget_krw"]  # backtest compounds; plan_orders' budget cap is for the live sleeve, so unset
    return cfg


# ---------- data (pykrx, lazy import) ----------

def connect(path: str):
    con = data.connect(path)
    con.executescript(SNAP_SCHEMA)
    return con


def snapshot_dates(cal: pd.DatetimeIndex, months: int) -> list[str]:
    """First trading day of every `months`-th month of the benchmark calendar."""
    first = pd.Series(cal, index=cal).groupby([cal.year, cal.month]).first()
    return [d.strftime("%Y-%m-%d") for k, d in enumerate(first) if k % months == 0]


def fetch_snapshots(con, dates: list[str], index_code: str, interval: float) -> None:
    from pykrx import stock
    have = {r[0] for r in con.execute("SELECT DISTINCT snap FROM constituents")}
    for d in dates:
        if d in have:
            continue
        members = list(stock.get_index_portfolio_deposit_file(index_code, d.replace("-", "")))
        if not members:  # empty = auth/session failure, never store it as "no members"
            raise RuntimeError(f"no constituents returned for {d}")
        with con:
            con.executemany("INSERT OR IGNORE INTO constituents VALUES (?,?)", [(d, t) for t in members])
        print(f"snapshot {d}: {len(members)} members")
        time.sleep(interval)


def backfill(con, tickers: list[str], start: str, end: str, interval: float) -> None:
    """Skips tickers already fetched through `end`; a crashed run resumes where it stopped. Upserts are idempotent."""
    from pykrx import stock
    done = dict(con.execute("SELECT ticker, through FROM fetched"))
    todo = [t for t in tickers if done.get(t) != end]
    for k, t in enumerate(todo, 1):
        df = stock.get_market_ohlcv(start.replace("-", ""), end.replace("-", ""), t, adjusted=data.ADJUSTED)
        rows = [] if df.empty else [
            (t, d.strftime("%Y-%m-%d"), r["시가"], r["고가"], r["저가"], r["종가"], int(r["거래량"]))
            for d, r in df[df["종가"] > 0].iterrows()]
        data.upsert(con, rows)
        with con:
            con.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?)", (t, end, len(rows)))
        if not rows:
            print(f"{t}: NO DATA (delisted/merged?)")
        if k % 25 == 0:
            print(f"backfill {k}/{len(todo)}")
        time.sleep(interval)


def load_panels(con, tickers: list[str], cal: pd.DatetimeIndex | None = None) -> dict:
    df = pd.read_sql("SELECT ticker, date, open, close, volume FROM ohlcv", con, parse_dates=["date"])
    df = df[df["ticker"].isin(tickers)]
    p = {c: df.pivot(index="date", columns="ticker", values=c) for c in ("open", "close", "volume")}
    if cal is not None:
        p = {c: v.reindex(cal) for c, v in p.items()}
    return p


def load_snaps(con) -> dict:
    snaps: dict = {}
    for s, t in con.execute("SELECT snap, ticker FROM constituents"):
        snaps.setdefault(s, set()).add(t)
    return snaps


# ---------- strategy ----------

def members_on(snaps: dict, d: pd.Timestamp) -> set:
    """Members of the latest snapshot taken on or before d (point in time); empty before the first."""
    ok = [s for s in snaps if s <= d.strftime("%Y-%m-%d")]
    return snaps[max(ok)] if ok else set()


def features(close: pd.DataFrame, volume: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Score per (date, ticker) = momentum if eligible (SMA fast > SMA slow, liquid), else NaN. Causal rolling ops only
    (truncation-tested). ponytail: no gap filling, so a halt makes a stock NaN/ineligible for the window length."""
    sma_f, sma_s = close.rolling(cfg["sma_fast"]).mean(), close.rolling(cfg["sma_slow"]).mean()
    liq = (close * volume).rolling(cfg["liq_window"]).mean()
    mom = close.shift(cfg["mom_skip"]) / close.shift(cfg["mom_lookback"] + cfg["mom_skip"]) - 1
    return mom.where((sma_f > sma_s) & (liq >= cfg["min_avg_value_krw"]))


def is_rebalance(cal: pd.DatetimeIndex, i: int) -> bool:
    """Last trading day of the ISO week (next bar is in a different week). The final bar has no fill day."""
    if i + 1 >= len(cal):
        return False
    return cal[i].isocalendar()[:2] != cal[i + 1].isocalendar()[:2]


def picks(score_row: pd.Series, members: set, n: int) -> list[str]:
    s = score_row[score_row.index.isin(members)].dropna()
    return list(s.sort_values(ascending=False).index[:n])


def run(panels: dict, snaps: dict, cfg: dict) -> dict:
    """Decide at the weekly last close, fill at the next bar's open. Only enter/leave names trade; stayers are untouched."""
    comm, tax, slip, n = cfg["commission"], cfg["sell_tax"], cfg["slippage"], cfg["n"]
    op, cl, vol = panels["open"], panels["close"], panels["volume"]
    cal = cl.index
    score = features(cl, vol, cfg)
    op = op.where(op > 0, cl)  # ponytail: open=0 bars (splits) fall back to that bar's close
    mark = cl.ffill()  # last known close for valuing a halted/delisted holding
    cash, shares, pending = float(cfg["initial_cash"]), {}, None
    equity, nhold, fills, skipped = [], [], [], []
    for i, d in enumerate(cal):
        if pending is not None:
            o = op.iloc[i]
            for t, q in list(shares.items()):  # leavers first (their proceeds fund entrants); a halted leaver stays held
                if t not in pending and o[t] == o[t]:
                    gross = q * o[t]
                    cash += gross * (1 - slip) * (1 - comm - tax)
                    fills.append(dict(i=i, t=t, side="sell", qty=q, notional=gross, cost=gross * (slip + comm + tax)))
                    del shares[t]
            eq = cash + sum(q * (o[t] if o[t] == o[t] else mark.iloc[i][t]) for t, q in shares.items())
            px = {t: o[t] * (1 + slip) for t in pending if o[t] == o[t] and t not in shares}
            bal = {"cash": cash, "orderable": cash, "equity": eq, "positions": {t: {"qty": q} for t, q in shares.items()}}
            orders, sk = backtest.plan_orders({t: 1 for t in px}, bal, px, cfg, n)
            skipped += [(d, t) for t in sk]
            for t, _, q in orders:
                cash -= q * px[t] * (1 + comm)
                shares[t] = q
                fills.append(dict(i=i, t=t, side="buy", qty=q, notional=q * o[t],
                                  cost=q * (px[t] - o[t]) + q * px[t] * comm))
            pending = None
        m = mark.iloc[i]
        equity.append(cash + sum(q * m[t] for t, q in shares.items()))
        nhold.append(len(shares))
        if is_rebalance(cal, i):
            pending = set(picks(score.iloc[i], members_on(snaps, d), n))
    return dict(equity=pd.Series(equity, index=cal), nhold=pd.Series(nhold, index=cal), fills=fills, skipped=skipped,
                shares=shares)


# ---------- report ----------

def segment(res: dict, bench: pd.DataFrame, cfg: dict, lo: int, hi: int) -> dict:
    eq = res["equity"]
    fl = [f for f in res["fills"] if lo <= f["i"] < hi]
    ev = backtest.evaluate(bench, dict(equity=eq, buys=[]), cfg, lo, hi)  # B&H of the benchmark, same capital + costs
    years = (eq.index[hi - 1] - eq.index[lo]).days / 365.25
    avg_eq = eq.iloc[lo:hi].mean()
    return dict({**ev["strategy"], "bh": ev["bh"]}, trades=len(fl), costs=sum(f["cost"] for f in fl),
                turnover=sum(f["notional"] for f in fl) / 2 / avg_eq / years,  # annualised one-way
                avg_hold=res["nhold"].iloc[lo:hi].mean(), start=eq.index[lo].date(), end=eq.index[hi - 1].date())


def report(res: dict, bench: pd.DataFrame, cfg: dict) -> None:
    cal = res["equity"].index
    lo_is = res["fills"][0]["i"]
    split = int(cal.searchsorted(pd.Timestamp(cfg["split_date"])))
    for name, lo, hi in (("IN-SAMPLE", lo_is, split), ("OUT-OF-SAMPLE", split, len(cal))):
        s = segment(res, bench, cfg, lo, hi)
        print(f"\n== {name} {s['start']} .. {s['end']} ==")
        print(f"{'':16}{'top-N':>12}{'KODEX200 B&H':>16}")
        for label, k, f in (("return", "ret", "{:+.1%}"), ("max drawdown", "maxdd", "{:.1%}"), ("sharpe (rf=0)", "sharpe", "{:.2f}")):
            print(f"{label:16}{f.format(s[k]):>12}{f.format(s['bh'][k]):>16}")
        print(f"{'turnover/yr':16}{s['turnover']:>11.1f}x\n{'trades':16}{s['trades']:>12}\n"
              f"{'costs paid KRW':16}{s['costs']:>12,.0f}\n{'avg holdings':16}{s['avg_hold']:>12.2f}")
    print(f"\nbuys skipped (rounded to 0 shares at {cfg['budget_krw']:,} KRW): {len(res['skipped'])}")


def current_picks(panels: dict, snaps: dict, cfg: dict) -> list[str]:
    score = features(panels["close"], panels["volume"], cfg)
    return picks(score.iloc[-1], members_on(snaps, score.index[-1]), cfg["n"])


def main(argv):
    from pykrx import stock
    cfg = load_cfg()
    con = connect(cfg["db_path"])
    end = (date.today() - timedelta(days=1)).isoformat()  # today's bar may be partial intraday
    if "--offline" not in argv:
        backfill(con, [cfg["benchmark"]], cfg["start"], end, cfg["call_interval"])
    bench = data.load(con, cfg["benchmark"])
    cal = bench.index
    if "--offline" not in argv:
        fetch_snapshots(con, snapshot_dates(cal, cfg["snapshot_months"]), cfg["index_code"], cfg["call_interval"])
    snaps = load_snaps(con)
    tickers = sorted(set().union(*snaps.values()) | {cfg["benchmark"]})
    if "--offline" not in argv:
        backfill(con, tickers, cfg["start"], end, cfg["call_interval"])
    nodata = [t for t, b in con.execute("SELECT ticker, bars FROM fetched") if b == 0]
    print(f"universe: {len(tickers)} tickers ever in KOSPI 200 since {cfg['start']}, {len(nodata)} with no price data: {nodata}")
    panels = load_panels(con, tickers, cal)
    res = run(panels, snaps, cfg)
    report(res, bench, cfg)
    print("\ncurrent top-N picks (as of", cal[-1].date(), "close):")
    for t in current_picks(panels, snaps, cfg):
        print(" ", t, stock.get_market_ticker_name(t))


if __name__ == "__main__":
    data.utf8_stdio()
    main(sys.argv)
