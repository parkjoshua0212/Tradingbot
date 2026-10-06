"""One-off KIS mock check: buy 1 share of the cheapest live ticker through the same path trading.py uses
(place_order, poll, book_order via trading.execute) and book it into the sleeve in state.json.
Never reads or writes last_run.txt. Run: python order_test.py --confirm"""
import json
import sys
import tomllib
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

import data
import kis_client
import messages
import trading
from notifier import send_alert


def check_window(now: datetime, holidays) -> None:
    if not trading.is_trading_day(now.date(), holidays):
        raise SystemExit("refusing: not a trading day")
    if not dtime(9, 0) <= now.time().replace(tzinfo=None) <= dtime(15, 20):
        raise SystemExit(f"refusing: {now:%H:%M} KST is outside 09:00-15:20")


def buy_one(client, cfg, now: datetime) -> str:
    """Returns the summary text. Raises SystemExit if nothing was ordered."""
    state = trading._load_state()
    sleeve = ({"cash": state["bot_cash"], "positions": dict(state["positions"]), "orders": state.get("orders", {})}
              if state else {"cash": cfg["budget_krw"], "positions": {}, "orders": {}})
    state = state or {}
    today_s = now.strftime("%Y%m%d")
    trading.sync_orders(client, sleeve, cfg, today_s)  # as a real run: book earlier late fills first
    prices = {t: client.get_price(t) for t in cfg["live_tickers"]}
    t = min(prices, key=prices.get)
    if sleeve["cash"] < prices[t] * (1 + cfg["cash_buffer"]):
        raise SystemExit(f"refusing: sleeve cash {sleeve['cash']:,.0f} cannot buy 1 x {t} at {prices[t]:,}")
    names = {tk: p["name"] for tk, p in client.get_balance()["positions"].items()}
    before = (sleeve["cash"], dict(sleeve["positions"]), trading.sleeve_equity(client, sleeve))
    old_orders = set(sleeve["orders"])

    def save(equity=state.get("equity"), peak=state.get("peak")):
        with open(trading.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"bot_cash": round(sleeve["cash"], 2), "equity": equity, "positions": sleeve["positions"],
                       "orders": sleeve["orders"], "peak": peak}, f)

    if not trading.execute(client, [(t, "buy", 1)], names, today_s, False, sleeve, cfg, save):
        raise SystemExit(f"order for {t} was rejected, see log; state.json unchanged")
    equity = trading.sleeve_equity(client, sleeve)
    save(equity, max(state.get("peak") or 0, before[2], equity))
    oid = (set(sleeve["orders"]) - old_orders).pop()
    o = sleeve["orders"][oid]
    return messages.format_order_test(oid, t, o["status"], o["booked_qty"], o["booked_value"],
                                      before, (sleeve["cash"], sleeve["positions"], equity))


if __name__ == "__main__":
    data.utf8_stdio()
    if "--confirm" not in sys.argv:
        raise SystemExit("refusing: pass --confirm to place a real order on the KIS mock")
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    check_window(now, cfg["holidays"])
    client = kis_client.KISClient(min_interval=cfg["kis_min_interval"])  # raises unless base_url is the mock
    assert client.base_url == kis_client.MOCK_URL
    text = buy_one(client, cfg, now)
    print(text)
    send_alert(text)
