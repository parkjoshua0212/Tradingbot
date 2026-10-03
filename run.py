import statistics
import sys
import tomllib

import pandas as pd

import backtest
import data

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


def row(s):
    return f"{s['ret']:8.1%} {s['maxdd']:8.1%} {s['sharpe']:6.2f}"


def med(xs):
    xs = [x for x in xs if x == x]
    return statistics.median(xs) if xs else float("nan")


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
