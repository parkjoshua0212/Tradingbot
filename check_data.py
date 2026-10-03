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
