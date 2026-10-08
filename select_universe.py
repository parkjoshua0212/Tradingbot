"""Live universe rule. Re-run after each KOSPI 200 rebalance (June and December), then edit config.toml by hand.

Rule, on one as-of date (point in time, KRX data):
  1. KOSPI 200 members (index 1028) on that date.
  2. Drop ETFs, preferred shares (code not ending in 0, or name ending 우/우B/우C) and halted names (zero volume that day).
  3. Drop close > MAX_PRICE (150,000: a 300k slice of the 3M budget buys at least 2 shares).
  4. Largest market cap first; at most PER_SECTOR names per KRX sector (업종명), with every finance sector
     (은행, 증권, 보험, 기타금융) counted as ONE sector. Stop at N.
Never ranks on past returns.

`python select_universe.py [YYYYMMDD]` (default: latest trading day) prints the picks, the skipped names ranked above
the last pick, a live_tickers line and a [names] skeleton. It never edits config.toml. It warns if the bot holds a
ticker that would be dropped: keep that one in live_tickers until the bot has sold it. Needs KRX_ID/KRX_PW in .env."""
import json
import sys

import pandas as pd

import data

N, MAX_PRICE, PER_SECTOR = 10, 150_000, 2
FINANCE = {"은행", "증권", "보험", "기타금융"}


def sector(name) -> str:
    name = str(name)
    return "금융" if name in FINANCE or "금융" in name else name


def select(df: pd.DataFrame, etfs: set, n: int = N) -> tuple[list[str], list[str]]:
    """df: KOSPI 200 members only, index = ticker, columns 종목명 업종명 종가 시가총액 거래량. Returns (picks, skip notes)."""
    picks, per, notes = [], {}, []
    for t, r in df.sort_values("시가총액", ascending=False).iterrows():
        s = sector(r["업종명"])
        why = ("ETF" if t in etfs else
               "preferred" if t[-1] != "0" or str(r["종목명"]).endswith(("우", "우B", "우C")) else
               "no volume (halted)" if r["거래량"] == 0 else
               f"close {r['종가']:,} > {MAX_PRICE:,}" if r["종가"] > MAX_PRICE else
               f"sector {s} already has {PER_SECTOR}" if per.get(s, 0) >= PER_SECTOR else None)
        if why:
            notes.append(f"{t} {r['종목명']}: {why}")
            continue
        per[s] = per.get(s, 0) + 1
        picks.append(t)
        if len(picks) == n:
            break
    return picks, notes


def main(argv):
    from pykrx import stock  # lazy: data has loaded .env (KRX login) by now

    day = argv[1] if len(argv) > 1 else stock.get_nearest_business_day_in_a_week()
    members = set(stock.get_index_portfolio_deposit_file("1028", day))
    if not members:  # empty = login failure or not a trading day; never select from nothing
        raise SystemExit(f"no KOSPI 200 members returned for {day}")
    cap = stock.get_market_cap(day, market="KOSPI")
    sec = stock.get_market_sector_classifications(day, "KOSPI")
    df = cap[cap.index.isin(members)].join(sec[["종목명", "업종명"]])
    picks, notes = select(df, set(stock.get_etf_ticker_list(day)))
    print(f"as of {day}: {len(members)} KOSPI 200 members\n")
    for k, t in enumerate(picks, 1):
        r = df.loc[t]
        print(f"{k:2} {t} {r['종목명']:<14} {r['업종명']:<10} ({sector(r['업종명'])})  close {r['종가']:>9,}  mcap {r['시가총액'] / 1e12:6.2f}T")
    print("\nskipped (ranked above the last pick):\n  " + "\n  ".join(notes))
    print(f"\nlive_tickers = {json.dumps(picks)}\n\n[names]")
    for t in picks:
        print(f'"{t}" = "<English> / {df.loc[t, "종목명"]}"')
    try:
        held = json.load(open("state.json", encoding="utf-8"))["positions"]
    except (FileNotFoundError, ValueError, KeyError):
        held = {}
    for t in sorted(set(held) - set(picks)):
        print(f"\nWARNING: the bot holds {held[t]} x {t}, which this rule drops. Keep it in live_tickers until it is sold.")


if __name__ == "__main__":
    data.utf8_stdio()
    main(sys.argv)
