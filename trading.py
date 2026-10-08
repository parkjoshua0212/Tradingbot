"""Daily mock-trading run: 20/60 SMA signal on the previous close, market orders after the open.
Run once per trading day shortly after 09:00 KST. `python trading.py --dry-run` sends no orders and writes no files."""
import json
import logging
import os
import sys
import time
import tomllib
from datetime import date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

import backtest
import data
import messages
import results
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


def sma_pairs(con, cfg, today: date) -> dict[str, tuple[float, float]]:
    """ticker -> (fast SMA, slow SMA) from bars strictly before today. Stale/short data is skipped."""
    out = {}
    for t in cfg["live_tickers"]:
        df = data.load(con, t)
        df = df[df.index < str(today)]
        if len(df) <= cfg["slow"] + 1 or (today - df.index[-1].date()).days > 5:
            log.warning("%s: no fresh signal (%d bars)", t, len(df))
            continue
        c = df["close"]
        f, s = c.rolling(cfg["fast"]).mean().iloc[-1], c.rolling(cfg["slow"]).mean().iloc[-1]
        if s == s:  # same rule as backtest.signals: long iff fast > slow
            out[t] = (f, s)
    return out


def target_positions(con, cfg, today: date) -> dict[str, int]:
    """ticker -> 1 (want long) / 0 (want flat)."""
    return {t: int(f > s) for t, (f, s) in sma_pairs(con, cfg, today).items()}


def signal_lines(pairs: dict, cfg: dict, names: dict) -> list[str]:
    """e.g. 'KEPCO / 한국전력: SMA20 32,100 < SMA60 33,400 -> flat' for every live ticker."""
    out = []
    for t in cfg["live_tickers"]:
        label = names.get(t, t)
        if t not in pairs:
            out.append(f"{label}: no fresh signal (stale or short data)")
            continue
        f, s = pairs[t]
        out.append(f"{label}: SMA{cfg['fast']} {f:,.0f} {'>' if f > s else '<' if f < s else '='} SMA{cfg['slow']} {s:,.0f}"
                   f" → {'long' if f > s else 'flat'}")
    return out


def _names(positions: dict, cfg: dict) -> dict:
    """Broker names for held tickers, overridden by the config.toml [names] table."""
    return {**{t: p["name"] for t, p in positions.items()}, **cfg.get("names", {})}


def _skip_row(dry_run, today: date, reason: str, budget: int):
    """A row for a day nothing ran, with whatever the last state.json knew."""
    if dry_run:
        return
    st = _load_state() or {}
    _log_results(lambda: results.log_daily(results.day_row(today.isoformat(), equity=st.get("equity"), cash=st.get("bot_cash"),
                                                           prev_equity=None, budget=budget, peak=st.get("peak"), notes=reason)))


def _log_results(fn):
    """Result logs must never break a run (e.g. the CSV is open in Excel and locked): alert and move on."""
    try:
        fn()
    except Exception as e:
        log.exception("result log failed")
        send_alert(messages.format_log_failed(e))


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


def book_order(sleeve, cfg, oid, st):
    """Book the not-yet-booked part of order `oid` from a broker status (cumulative filled qty and avg price).
    Idempotent: only the increase over what was already booked is applied. Sets the order's status."""
    o = sleeve["orders"][oid]
    d = st["filled"] - o["booked_qty"]
    if d > 0:
        value = st["filled"] * st["avg_price"]
        apply_fill(sleeve, cfg, o["ticker"], o["side"], d, (value - o["booked_value"]) / d)
        o["booked_qty"], o["booked_value"] = st["filled"], value
    o["status"] = ("filled" if st["filled"] >= o["qty"] else "cancelled" if not st["open"]
                   else "partial" if st["filled"] else "pending")


def sync_orders(client, sleeve, cfg, today_s):
    """Book late/partial fills of saved orders that are still open. A day order still open after its day is expired."""
    for oid, o in sleeve["orders"].items():
        if o["status"] in ("filled", "cancelled"):
            continue
        st = client.get_order_status(oid, o["day"])
        if st:
            book_order(sleeve, cfg, oid, st)
            if o["status"] in ("pending", "partial") and o["day"] < today_s:
                o["status"] = "cancelled"


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def execute(client, orders, names, today_s, dry_run, sleeve, cfg, save) -> int:
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
        sleeve["orders"][oid] = {"ticker": t, "side": side, "qty": qty, "day": today_s, "status": "pending",
                                 "booked_qty": 0, "booked_value": 0.0}
        save()  # persist the order number before anything else can fail
        send_alert(messages.format_order_submitted(t, name, side, qty))
        st = None
        for _ in range(5):  # ponytail: fixed 10s poll; an open/partial order is reported, not chased
            time.sleep(2)
            st = client.get_order_status(oid, today_s)
            if st and not st["open"]:
                break
        if st:  # anything filling after this poll is booked by sync_orders on the next run
            book_order(sleeve, cfg, oid, st)
            save()
        if st and st["filled"] == qty:
            send_alert(messages.format_order_filled(t, name, side, qty, st["avg_price"]))
        else:
            send_alert(messages.format_order_pending(t, name, side, qty))
    return n


def main(argv, now: datetime | None = None):
    dry_run = "--dry-run" in argv
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))  # KST regardless of the PC's timezone
    today = now.date()
    today_s = today.strftime("%Y%m%d")
    if not is_trading_day(today, cfg["holidays"]):
        log.info("not a trading day, nothing to do")
        _skip_row(dry_run, today, "not a trading day (weekend/holiday)", cfg["budget_krw"])
        return
    start, end = (dtime.fromisoformat(cfg[k]) for k in ("trade_window_start", "trade_window_end"))
    if not dry_run and not start <= now.time().replace(tzinfo=None) <= end:
        log.error("outside trade window %s-%s KST (now %s); no orders", start, end, now.strftime("%H:%M:%S"))
        send_alert(messages.format_outside_window(now.strftime("%H:%M"), cfg["trade_window_start"], cfg["trade_window_end"]))
        _skip_row(dry_run, today, f"outside trade window {start:%H:%M}-{end:%H:%M} (started {now:%H:%M})", cfg["budget_krw"])
        return
    client = KISClient(min_interval=cfg["kis_min_interval"])  # raises if not the mock URL
    if not dry_run and os.path.exists(LAST_RUN_FILE) and open(LAST_RUN_FILE).read().strip() == today_s:
        log.error("already ran today (%s); delete %s to override", today_s, LAST_RUN_FILE)
        return

    bal = client.get_balance()  # source of truth for positions; cash and P&L are the bot's own sleeve
    state = _load_state()
    budget = cfg["budget_krw"]
    sleeve = ({"cash": state["bot_cash"], "positions": dict(state["positions"]), "orders": state.get("orders", {})} if state
              else {"cash": budget, "positions": {}, "orders": {}})
    state = state or {}

    def save(equity=state.get("equity"), peak=state.get("peak")):
        if not dry_run:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"bot_cash": round(sleeve["cash"], 2), "equity": equity, "positions": sleeve["positions"],
                           "orders": sleeve["orders"], "peak": peak, "deposits": state.get("deposits", [])}, f)

    sync_orders(client, sleeve, cfg, today_s)  # before reconcile: late fills would otherwise look like mismatches
    old_ids, lines = set(sleeve["orders"]), []
    diffs = reconcile(sleeve, bal, cfg["live_tickers"])
    if diffs:
        log.warning("reconciliation mismatch: %s", diffs)
        send_alert(messages.format_reconcile_mismatch(diffs))

    n = 0
    equity0 = equity = sleeve_equity(client, sleeve)
    names = _names(bal["positions"], cfg)
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
        pairs = sma_pairs(con, cfg, today)
        targets = {t: int(f > s) for t, (f, s) in pairs.items()}
        lines = signal_lines(pairs, cfg, names)
        for line in lines:
            log.info(line)
        prices = {t: client.get_price(t) for t, w in targets.items() if w == 1 and t not in sleeve["positions"]}
        sleeve_bal = {"cash": sleeve["cash"], "orderable": bal["orderable"], "equity": equity,  # plan_orders takes the min
                      "positions": {t: {"qty": q} for t, q in sleeve["positions"].items()}}
        orders, skipped = plan_orders(targets, sleeve_bal, prices, cfg, len(cfg["live_tickers"]))
        log.info("planned orders: %s; skipped (0 shares): %s", orders, skipped)
        for t in skipped:
            send_alert(messages.format_skipped_zero_shares(t, prices[t]))
        if not dry_run:
            with open(LAST_RUN_FILE, "w") as f:
                f.write(today_s)
        n = execute(client, orders, names, today_s, dry_run, sleeve, cfg, save)

    if n:
        equity = sleeve_equity(client, sleeve)
        names.update(_names(client.get_balance()["positions"], cfg))
    peak = max(state.get("peak") or 0, equity0, equity)
    save(equity, peak)
    if not dry_run:
        new = [sleeve["orders"][i] for i in sleeve["orders"] if i not in old_ids]
        notes = "; ".join(([reason] if reason else []) + [f"mismatch: {d}" for d in diffs] +
                          [f"{o['side']} {o['ticker']} {o['status']}" for o in new if o["status"] != "filled"])
        _log_results(lambda: results.log_trades(sleeve["orders"], cfg, names, now))
        _log_results(lambda: results.log_daily(results.day_row(
            today.isoformat(), equity=equity, cash=sleeve["cash"], prev_equity=state.get("equity") or budget, budget=budget,
            peak=peak, sent=n, filled=sum(o["status"] == "filled" for o in new), kill=bool(reason), reconcile_ok=not diffs,
            notes=notes)))
    held = {t: {"name": names.get(t, t), "qty": q} for t, q in sleeve["positions"].items()}
    send_alert(messages.format_eod_summary(equity, round(sleeve["cash"]), held, n, budget, dry_run, lines))


def keep_awake(on: bool):
    """Windows only: stop the PC sleeping mid-run (ES_CONTINUOUS | ES_SYSTEM_REQUIRED); no-op elsewhere."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))


if __name__ == "__main__":
    data.utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    keep_awake(True)
    try:
        main(sys.argv)
    except Exception as e:
        log.exception("unhandled error")
        send_alert(messages.format_error(e))
        raise
    finally:
        keep_awake(False)
