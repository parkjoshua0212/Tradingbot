import json
import logging
import statistics
import sys
import tomllib

import pandas as pd

import backtest
import data
import messages
from notifier import send_alert


def row(s):
    return f"{s['ret']:8.1%} {s['maxdd']:8.1%} {s['sharpe']:6.2f}"


def med(xs):
    xs = [x for x in xs if x == x]
    return statistics.median(xs) if xs else float("nan")


def portfolio_report(con, cfg, save_to=None):
    """Live sizing rules (one cash pool, equal slices, integer shares, buffer) on the live universe: the paper-trading baseline."""
    cfg = {**cfg, "initial_cash": cfg.get("budget_krw", cfg["initial_cash"])}
    dfs = {t: data.load(con, t) for t in cfg["live_tickers"]}
    strat, bh = (backtest.run_portfolio(dfs, cfg, always_long=x) for x in (False, True))
    split = pd.Timestamp(cfg["split_date"])
    print(f"\n=== live-sizing portfolio {cfg['live_tickers']} ===")
    print(f"{'':24}{'ret':>9}{'maxDD':>9}{'sharpe':>7}{'buys':>5}")
    out = {}
    for label, res in (("strategy", strat), ("equal-wt B&H", bh)):
        eq = res["equity"]
        for per, part in (("all", eq), ("out-of-sample", eq[eq.index >= split])):
            base = cfg["initial_cash"] if per == "all" else eq[eq.index < split].iloc[-1]
            r = backtest._stats(part, base)
            out[f"{label} {per}"] = {**{k: float(v) for k, v in r.items()}, "final_equity": float(part.iloc[-1])}
            print(f"{label + ' ' + per:24}{r['ret']:8.1%} {r['maxdd']:8.1%} {r['sharpe']:6.2f}{res['buys'] if label == 'strategy' and per == 'all' else '':>5}")
    zero = sorted({t for _, t in strat["skipped"]})
    if zero:
        print(f"rounded to 0 shares at least once: {zero}")
    if save_to:
        keys = ("live_tickers", "budget_krw", "fast", "slow", "commission", "sell_tax", "slippage", "cash_buffer", "split_date")
        with open(save_to, "w", encoding="utf-8") as f:
            json.dump({"config": {k: cfg.get(k) for k in keys}, "period": [str(strat["equity"].index[0].date()), str(strat["equity"].index[-1].date())],
                       "buys": strat["buys"], "zero_share_tickers": zero, "results": out}, f, indent=2)
        print(f"saved {save_to}")


def main():
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)

    con = data.connect(cfg["db_path"])
    if "--offline" not in sys.argv:
        data.backfill(con, cfg["tickers"], cfg["start"], cfg.get("end"))

    results = {"in-sample": {}, "out-of-sample": {}}
    for t in cfg["tickers"]:
        df = data.load(con, t)
        if len(df) <= cfg["slow"] + 1:
            print(f"{t}: not enough data ({len(df)} bars)")
            continue
        res = backtest.run(df, cfg)
        split = int(df.index.searchsorted(pd.Timestamp(cfg["split_date"])))  # first bar >= split
        for name, (lo, hi) in {"in-sample": (cfg["slow"], split), "out-of-sample": (split, len(df))}.items():
            r = backtest.evaluate(df, res, cfg, lo, hi)
            if r:
                results[name][t] = r

    for name, rs in results.items():
        print(f"\n=== {name} (split {cfg['split_date']}) ===")
        print(f"{'ticker':8}{'ret':>9}{'maxDD':>9}{'sharpe':>7}{'buys':>5}  |  B&H{'ret':>5}{'maxDD':>9}{'sharpe':>7}")
        for t, r in rs.items():
            print(f"{t:8}{row(r['strategy'])}{r['strategy']['trades']:5d}  |      {row(r['bh'])}")
        if rs:
            s = [r["strategy"] for r in rs.values()]
            b = [r["bh"] for r in rs.values()]
            beat = sum(x["sharpe"] > y["sharpe"] for x, y in zip(s, b))
            print(f"{'MEDIAN':8}{med(x['ret'] for x in s):8.1%} {'':8} {med(x['sharpe'] for x in s):6.2f}"
                  f"{'':5}  |      {med(x['ret'] for x in b):8.1%} {'':8} {med(x['sharpe'] for x in b):6.2f}")
            print(f"strategy beat B&H on Sharpe: {beat}/{len(rs)} tickers")
    portfolio_report(con, cfg, "baseline.json" if "--save-baseline" in sys.argv else None)
    send_alert(messages.format_backtest_summary(results))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--test-alert" in sys.argv:
        send_alert(messages.format_test_alert())
        sys.exit()
    try:
        main()
    except Exception as e:
        send_alert(messages.format_error(e))
        raise
