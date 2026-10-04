"""Daily mock-trading run: 20/60 SMA signal on the previous close, market orders after the open.
Run once per trading day shortly after 09:00 KST. `python trading.py --dry-run` sends no orders and writes no files."""
import json
import logging
import os
import sys
import time
import tomllib
from datetime import date

import backtest
import data
import messages
from backtest import plan_orders  # shared with the portfolio backtest: one set of sizing rules
from kis_client import KISClient, KISError
from notifier import send_alert

log = logging.getLogger(__name__)
KILL_FILE, STATE_FILE, LAST_RUN_FILE = "KILL", "state.json", "last_run.txt"


def is_trading_day(d: date, holidays) -> bool:
    """Weekends + configured KRX holidays. Refuses years the list does not cover (a stale list must not trade blindly)."""
    if not any(h.startswith(str(d.year)) for h in holidays):
        raise ValueError(f"config.toml holidays has no entries for {d.year}; update the list")
    return d.weekday() < 5 and d.isoformat() not in holidays


def target_positions(con, cfg, today: date) -> dict[str, int]:
    """ticker -> 1 (want long) / 0 (want flat), from bars strictly before today. Stale/short data is skipped."""
    out = {}
    for t in cfg["live_tickers"]:
        df = data.load(con, t)
        df = df[df.index < str(today)]
        if len(df) <= cfg["slow"] + 1 or (today - df.index[-1].date()).days > 5:
            log.warning("%s: no fresh signal (%d bars)", t, len(df))
            continue
        s = backtest.signals(df["close"], cfg["fast"], cfg["slow"]).iloc[-1]
        if s == s:
            out[t] = int(s)
    return out


def kill_reason(equity, last_equity, peak, cfg, kill_file=KILL_FILE) -> str | None:
    """KILL file, daily loss vs the previous run, or drawdown vs peak equity since start."""
    if os.path.exists(kill_file):
        return f"{kill_file} file exists"
    loss = cfg["max_daily_loss_pct"]
    if last_equity and equity < last_equity * (1 - loss / 100):
        return f"equity {equity:,} is {1 - equity / last_equity:.1%} below previous run {last_equity:,} (limit {loss}%)"
    if peak and equity < peak * (1 - cfg["max_drawdown"]):
        return f"equity {equity:,} is {1 - equity / peak:.1%} below peak {peak:,} (limit {cfg['max_drawdown']:.0%})"
    return None


def reconcile(sleeve: dict, actual: dict, live: list, cash_tol=1000) -> list[str]:
    """Bot's own positions vs the broker's, live tickers only (the account holds other things too)."""
    diffs = []
    exp, act = sleeve["positions"], {t: p["qty"] for t, p in actual["positions"].items() if t in live}
    for t in sorted(exp.keys() | act.keys()):
        e, a = exp.get(t, 0), act.get(t, 0)
        if e == a:
            continue
        diffs.append(f"{t}: broker holds {a} share the bot did not create" if e == 0
                     else f"{t}: expected {e} share, broker has {a}")
    if sleeve["cash"] > actual["orderable"] + cash_tol:
        diffs.append(f"bot cash {sleeve['cash']:,.0f} exceeds broker orderable cash {actual['orderable']:,}")
    return diffs


def sleeve_equity(client, sleeve) -> int:
    """Bot cash + market value of the bot's own positions: the number the kill switch and P&L use."""
    return round(sleeve["cash"] + sum(q * client.get_price(t) for t, q in sleeve["positions"].items()))


def apply_fill(sleeve, cfg, ticker, side, qty, price):
    """Book a fill into the sleeve: price x qty plus commission (buy) or minus commission and tax (sell)."""
    pos = sleeve["positions"]
    if side == "buy":
        sleeve["cash"] -= qty * price * (1 + cfg["commission"])
        pos[ticker] = pos.get(ticker, 0) + qty
    else:
        sleeve["cash"] += qty * price * (1 - cfg["commission"] - cfg["sell_tax"])
        if pos.get(ticker, 0) - qty > 0:
            pos[ticker] -= qty
        else:
            pos.pop(ticker, None)


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def execute(client, orders, names, today_s, dry_run, sleeve, cfg) -> int:
    n = 0
    for t, side, qty in orders:
        name = names.get(t, t)
        if dry_run:
            log.info("DRY RUN: would %s %d x %s", side, qty, t)
            continue
        try:
            oid = client.place_order(t, side, qty, "market")  # sent once, never retried
        except KISError as e:
            log.error("order rejected %s %s %d: %s", side, t, qty, e)
            send_alert(messages.format_order_rejected(t, name, side, qty, str(e)))
            continue
        n += 1
        send_alert(messages.format_order_submitted(t, name, side, qty))
        st = None
        for _ in range(5):  # ponytail: fixed 10s poll; an open/partial order is reported, not chased
            time.sleep(2)
            st = client.get_order_status(oid, today_s)
            if st and not st["open"]:
                break
        if st and st["filled"] > 0:  # a late fill of the rest is not seen here; reconcile flags it next run
            apply_fill(sleeve, cfg, t, side, st["filled"], st["avg_price"])
        if st and st["filled"] == qty:
            send_alert(messages.format_order_filled(t, name, side, qty, st["avg_price"]))
        else:
            send_alert(messages.format_order_pending(t, name, side, qty))
    return n


def main(argv, today: date | None = None):
    dry_run = "--dry-run" in argv
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    today = today or date.today()
    today_s = today.strftime("%Y%m%d")
    if not is_trading_day(today, cfg["holidays"]):
        log.info("not a trading day, nothing to do")
        return
    client = KISClient(min_interval=cfg["kis_min_interval"])  # raises if not the mock URL
    if not dry_run and os.path.exists(LAST_RUN_FILE) and open(LAST_RUN_FILE).read().strip() == today_s:
        log.error("already ran today (%s); delete %s to override", today_s, LAST_RUN_FILE)
        return

    bal = client.get_balance()  # source of truth for positions; cash and P&L are the bot's own sleeve
    state = _load_state()
    budget = cfg["budget_krw"]
    sleeve = ({"cash": state["bot_cash"], "positions": dict(state["positions"])} if state
              else {"cash": budget, "positions": {}})
    state = state or {}
    diffs = reconcile(sleeve, bal, cfg["live_tickers"])
    if diffs:
        log.warning("reconciliation mismatch: %s", diffs)
        send_alert(messages.format_reconcile_mismatch(diffs))

    n = 0
    equity0 = equity = sleeve_equity(client, sleeve)
    names = {t: p["name"] for t, p in bal["positions"].items()}
    reason = kill_reason(equity, state.get("equity"), state.get("peak"), cfg)
    if reason:
        log.error("kill switch: %s", reason)
        send_alert(messages.format_kill_switch(reason))
        if not dry_run and not os.path.exists(KILL_FILE):
            open(KILL_FILE, "w").write(reason)  # sticky: delete KILL to resume
    else:
        con = data.connect(cfg["db_path"])
        if "--offline" not in argv:
            data.backfill(con, cfg["live_tickers"], cfg["start"], cfg.get("end"))
        targets = target_positions(con, cfg, today)
        prices = {t: client.get_price(t) for t, w in targets.items() if w == 1 and t not in sleeve["positions"]}
        sleeve_bal = {"cash": sleeve["cash"], "orderable": min(sleeve["cash"], bal["orderable"]), "equity": equity,
                      "positions": {t: {"qty": q} for t, q in sleeve["positions"].items()}}
        orders, skipped = plan_orders(targets, sleeve_bal, prices, cfg, len(cfg["live_tickers"]))
        log.info("planned orders: %s; skipped (0 shares): %s", orders, skipped)
        for t in skipped:
            send_alert(messages.format_skipped_zero_shares(t, prices[t]))
        if not dry_run:
            with open(LAST_RUN_FILE, "w") as f:
                f.write(today_s)
        n = execute(client, orders, names, today_s, dry_run, sleeve, cfg)

    if n:
        equity = sleeve_equity(client, sleeve)
        names.update({t: p["name"] for t, p in client.get_balance()["positions"].items()})
    if not dry_run:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"bot_cash": round(sleeve["cash"], 2), "equity": equity, "positions": sleeve["positions"],
                       "peak": max(state.get("peak") or 0, equity0, equity)}, f)
    held = {t: {"name": names.get(t, t), "qty": q} for t, q in sleeve["positions"].items()}
    send_alert(messages.format_eod_summary(equity, round(sleeve["cash"]), held, n, budget, dry_run))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main(sys.argv)
    except Exception as e:
        log.exception("unhandled error")
        send_alert(messages.format_error(e))
        raise
