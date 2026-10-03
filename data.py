import os
import sqlite3
from datetime import date

import pandas as pd

# Explicit, not the library default. pykrx adjusted=True -> Naver Finance 수정주가
# (split/capital-change adjusted; NOT cash-dividend adjusted, so returns exclude dividends).
# adjusted=False -> KRX raw prices, which need KRX_ID/KRX_PW. Changing this means rebuilding the DB.
ADJUSTED = True

SCHEMA = """CREATE TABLE IF NOT EXISTS ohlcv (
    ticker TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (ticker, date))"""

UPSERT = """INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?)
ON CONFLICT(ticker, date) DO UPDATE SET
    open=excluded.open, high=excluded.high, low=excluded.low,
    close=excluded.close, volume=excluded.volume"""


def load_env(path: str = ".env") -> None:
    """Minimal .env reader (no python-dotenv). Real env vars win. Must run before pykrx is imported."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    return con


def upsert(con: sqlite3.Connection, rows: list[tuple]) -> None:
    with con:
        con.executemany(UPSERT, rows)


def backfill(con: sqlite3.Connection, tickers: list[str], start: str, end: str | None = None) -> None:
    """Refetch the full [start, end] window and upsert. Idempotent, and a re-run
    also heals gaps. ponytail: full refetch per run, fetch only from last stored date if slow."""
    from pykrx import stock  # lazy: keeps tests hermetic, and .env is loaded first

    end = end or date.today().isoformat()
    s, e = start.replace("-", ""), end.replace("-", "")
    for t in tickers:
        df = stock.get_market_ohlcv(s, e, t, adjusted=ADJUSTED)  # cols: 시가 고가 저가 종가 거래량
        df = df[df["종가"] > 0]  # halted days come back as zero rows
        rows = [
            (t, d.strftime("%Y-%m-%d"), r["시가"], r["고가"], r["저가"], r["종가"], int(r["거래량"]))
            for d, r in df.iterrows()
        ]
        upsert(con, rows)
        print(f"{t}: upserted {len(rows)} bars")


def load(con: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM ohlcv WHERE ticker=? ORDER BY date", con, params=(ticker,), parse_dates=["date"]
    ).set_index("date")
