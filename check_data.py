"""Eyeball the DB against Naver Finance and flag unadjusted-looking price jumps."""
import tomllib

import data

LIMIT = 0.30  # KRX daily price limit; a bigger close-to-close move means a split/event wasn't adjusted
DATES = ["2021-04-01", "2021-04-15", "2021-04-30"]

with open("config.toml", "rb") as f:
    cfg = tomllib.load(f)
con = data.connect(cfg["db_path"])

print(f"adjusted={data.ADJUSTED} -> pykrx source: {'Naver Finance (split-adjusted, no KRX login needed)' if data.ADJUSTED else 'KRX raw (login required)'}")
from pykrx.website.comm import auth  # noqa: E402  (after load_env so KRX_ID/KRX_PW are seen)

print(f"KRX login: {'ok' if auth.get_auth_session() else 'not logged in (fine for adjusted=True)'}")

flagged = 0
print(f"\n{'ticker':8}{'first':>12}{'close':>10}{'last':>12}{'close':>10}  " + "".join(f"{d:>12}" for d in DATES))
for t in cfg["tickers"]:
    df = data.load(con, t)
    if df.empty:
        print(f"{t:8}NO DATA")
        continue
    at = [f"{df['close'].get(d, float('nan')):12,.0f}" for d in map(__import__('pandas').Timestamp, DATES)]
    print(f"{t:8}{df.index[0].date()!s:>12}{df['close'].iloc[0]:10,.0f}{df.index[-1].date()!s:>12}{df['close'].iloc[-1]:10,.0f}  " + "".join(at))
    chg = df["close"].pct_change()
    for d, c in chg[chg.abs() > LIMIT].items():
        flagged += 1
        print(f"  !! {t} {d.date()} close {df['close'].shift(1)[d]:,.0f} -> {df['close'][d]:,.0f} ({c:+.0%})  likely unadjusted event")
print(f"\n{flagged} day(s) beyond ±{LIMIT:.0%}" + (" — data NOT clean" if flagged else " — OK"))

# --- spot_check.csv: fill my_naver_close / match by hand against stock.naver.com ---
import csv  # noqa: E402

import pandas as pd  # noqa: E402
from pykrx import stock  # noqa: E402

rows = []
for t in cfg["tickers"]:
    df = data.load(con, t)
    if df.empty:
        continue
    try:
        name = stock.get_market_ticker_name(t)
        name = name if isinstance(name, str) else ""
    except Exception:
        name = ""
    asked = [(df.index[0], ""), (df.index[-1], "")] + [(pd.Timestamp(d), d) for d in DATES]
    for want, req in asked:
        prior = df.loc[:want]  # nearest earlier trading day
        if prior.empty:
            rows.append([t, name, req, "", "", "", f"no bar on/before {req}"])
            continue
        got = prior.index[-1]
        note = f"{req} not a trading day, used earlier day" if req and got != want else ""
        rows.append([t, name, got.date().isoformat(), f"{prior['close'].iloc[-1]:.0f}", "", "", note])

with open("spot_check.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["ticker", "name", "date", "close", "my_naver_close", "match", "note"])
    w.writerows(rows)
print(f"wrote spot_check.csv ({len(rows)} rows)")
