"""Excel-friendly result logs in results/ (gitignored): UTF-8 with BOM, append-only, never the same day or fill twice.
`python results.py` backfills trades.csv (and today's daily row) from state.json; safe to repeat."""
import csv
import json
import os
import tomllib
from datetime import datetime

DIR = "results"
DAILY, TRADES = os.path.join(DIR, "daily_results.csv"), os.path.join(DIR, "trades.csv")
DAILY_COLS = ["date", "sleeve_equity", "bot_cash", "positions_value", "daily_pnl", "daily_pnl_pct", "total_pnl_vs_budget",
              "peak_equity", "drawdown_pct", "orders_sent", "orders_filled", "kill_switch_active", "reconcile_ok", "notes"]
TRADE_COLS = ["date", "time", "ticker", "name", "side", "qty", "avg_price", "value", "commission", "tax", "order_no", "source"]


def text(v: str) -> str:
    """Excel keeps leading zeros for ="005930"."""
    return f'="{v}"'


def untext(v: str) -> str:
    return v[2:-1] if v.startswith('="') else v


def _read(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _append(path: str, cols: list[str], row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:  # BOM only at the file start
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        w.writerow([row.get(c, "") for c in cols])


def day_row(date_s: str, *, equity, cash, prev_equity, budget, peak, sent=0, filled=0, kill=False,
            reconcile_ok=True, notes="") -> dict:
    """Any number may be None (skipped days know little); it is written as an empty cell."""
    r = lambda x, n=None: "" if x is None else round(x, n)  # noqa: E731
    pnl = None if equity is None or not prev_equity else equity - prev_equity
    return {"date": date_s, "sleeve_equity": r(equity), "bot_cash": r(cash),
            "positions_value": r(None if equity is None or cash is None else equity - cash),
            "daily_pnl": r(pnl), "daily_pnl_pct": r(None if pnl is None else pnl / prev_equity * 100, 2),
            "total_pnl_vs_budget": r(None if equity is None else equity - budget), "peak_equity": r(peak),
            "drawdown_pct": r(None if equity is None or not peak else (1 - equity / peak) * 100, 2),
            "orders_sent": sent, "orders_filled": filled, "kill_switch_active": kill, "reconcile_ok": reconcile_ok,
            "notes": notes}


def log_daily(row: dict) -> bool:
    """False (nothing written) if that date already has a row."""
    if any(r["date"] == row["date"] for r in _read(DAILY)):
        return False
    _append(DAILY, DAILY_COLS, row)
    return True


def log_trades(orders: dict, cfg: dict, names: dict, now: datetime, live: bool = True) -> int:
    """One row per booked fill increment. Idempotent: an order's logged qty is compared with its booked_qty, so a
    partial fill logs once and the rest later; re-running (or backfilling) writes nothing new."""
    rows, n = _read(TRADES), 0
    for oid, o in orders.items():
        mine = [r for r in rows if untext(r["order_no"]) == oid]
        qty = o["booked_qty"] - sum(int(r["qty"]) for r in mine)
        if qty <= 0:
            continue
        value = o["booked_value"] - sum(float(r["value"]) for r in mine)
        sell = o["side"] == "sell"
        timed = live and o["day"] == now.strftime("%Y%m%d")  # a late or backfilled fill has no known time
        _append(TRADES, TRADE_COLS, {
            "date": f"{o['day'][:4]}-{o['day'][4:6]}-{o['day'][6:]}", "time": now.strftime("%H:%M:%S") if timed else "",
            "ticker": text(o["ticker"]), "name": names.get(o["ticker"], ""), "side": o["side"], "qty": qty,
            "avg_price": round(value / qty, 2), "value": round(value, 2), "commission": round(value * cfg["commission"], 2),
            "tax": round(value * cfg["sell_tax"], 2) if sell else 0, "order_no": text(oid), "source": o.get("source", "trading")})
        n += 1
    return n


def backfill(state: dict, cfg: dict, last_run: str | None, names: dict, now: datetime) -> None:
    n = log_trades(state.get("orders", {}), cfg, names, now, live=False)
    print(f"trades.csv: {n} new rows")
    if last_run:
        day = f"{last_run[:4]}-{last_run[4:6]}-{last_run[6:]}"
        mine = [o for o in state.get("orders", {}).values() if o["day"] == last_run]
        sent = [o for o in mine if o.get("source") != "order_test"]
        extra = "; includes order_test buy" if len(mine) > len(sent) else ""
        row = day_row(day, equity=state.get("equity"), cash=state.get("bot_cash"), prev_equity=None, budget=cfg["budget_krw"],
                      peak=state.get("peak"), sent=len(sent), filled=sum(o["status"] == "filled" for o in sent),
                      notes=f"backfilled from state.json (end-of-today values{extra})")
        print("daily_results.csv:", "row added" if log_daily(row) else f"{day} already logged")


if __name__ == "__main__":
    import data
    import kis_client
    data.utf8_stdio()
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    state = json.load(open("state.json", encoding="utf-8"))
    try:  # names of held tickers come from the broker (read-only call); a sold ticker just gets an empty name
        names = {t: p["name"] for t, p in kis_client.KISClient(min_interval=cfg["kis_min_interval"]).get_balance()["positions"].items()}
    except Exception as e:
        print("names unavailable:", e)
        names = {}
    last = open("last_run.txt").read().strip() if os.path.exists("last_run.txt") else None
    backfill(state, cfg, last, names, datetime.now())
