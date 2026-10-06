"""All Discord-facing text. Plain and short; logs elsewhere stay plain too."""


def format_backtest_summary(results: dict) -> str:
    """results: {period: {ticker: {"strategy": {...}, "bh": {...}}}} as built in run.py."""
    import statistics

    def med(xs):
        xs = [x for x in xs if x == x]
        return statistics.median(xs) if xs else float("nan")

    lines = ["📊 **Backtest results**"]
    for name, rs in results.items():
        if not rs:
            lines.append(f"• {name}: no data.")
            continue
        s = med(r["strategy"]["sharpe"] for r in rs.values())
        b = med(r["bh"]["sharpe"] for r in rs.values())
        verdict = "✅ Strategy beat buy-and-hold." if s > b else "❌ Strategy lost to buy-and-hold."
        lines.append(f"• **{name}**: strategy Sharpe {s:.2f} vs B&H {b:.2f} (median, {len(rs)} tickers). {verdict}")
    return "\n".join(lines)


def format_error(exc: BaseException) -> str:
    return f"🚨 **Bot error** — something broke.\n`{type(exc).__name__}: {exc}`"[:1900]


def format_order_filled(ticker: str, name: str, side: str, qty: int, price: float) -> str:
    verb = "Bought" if side.lower() == "buy" else "Sold"
    return f"💼 **Filled** — {verb} {qty:,} x {name} ({ticker}) at ₩{price:,.0f}."


def format_order_submitted(ticker: str, name: str, side: str, qty: int) -> str:
    return f"📨 **Order sent** — {side} {qty:,} x {name} ({ticker})."


def format_order_rejected(ticker: str, name: str, side: str, qty: int, reason: str) -> str:
    return f"❌ **Order rejected** — {side} {qty:,} x {name} ({ticker}). Reason: {reason[:300]}"


def format_order_pending(ticker: str, name: str, side: str, qty: int) -> str:
    return f"⏳ **Not filled yet** — {side} {qty:,} x {name} ({ticker}). It will be checked next run."


def format_skipped_zero_shares(ticker: str, price: float) -> str:
    return f"⚠️ **Skipped buy** — 1 share of {ticker} (₩{price:,.0f}) costs more than its budget slice."


def format_kill_switch(reason: str) -> str:
    return f"🛑 **Trading stopped** — {reason[:500]}"


def format_reconcile_mismatch(diffs: list[str]) -> str:
    body = "\n".join(f"• {d}" for d in diffs)
    return f"🔍 **Mismatch** — bot records differ from the broker (broker is right):\n{body}"[:1900]


def format_eod_summary(equity: int, cash: int, positions: dict, n_orders: int, budget: int, dry_run: bool = False,
                       signals: list[str] = ()) -> str:
    """equity/cash are the bot's sleeve, not the broker account; P&L is vs the starting budget."""
    held = ", ".join(f"{p['name']} x{p['qty']}" for p in positions.values()) or "none"
    tag = " (dry run, no orders sent)" if dry_run else ""
    pnl = equity - budget
    return (f"📈 **Daily summary**{tag} — bot money ₩{equity:,} ({pnl:+,} / {pnl / budget:+.1%} vs budget ₩{budget:,}), "
            f"cash ₩{cash:,}. {n_orders} orders today. Holding: {held}." + "".join("\n" + line for line in signals))[:1900]


def format_outside_window(now: str, start: str, end: str) -> str:
    return f"⏰ **No orders** — bot started at {now} KST, outside the trade window {start}-{end}. Nothing was sent."


def format_test_alert() -> str:
    return "🔔 **Test** — Discord alerts are working!"


def format_order_test(oid: str, ticker: str, status: str, qty: int, value: float, before: tuple, after: tuple) -> str:
    """before/after = (sleeve cash, {ticker: qty}, sleeve equity)."""
    def sleeve(s):
        held = ", ".join(f"{t} x{q}" for t, q in s[1].items()) or "none"
        return f"cash ₩{s[0]:,.0f}, holding {held}, equity ₩{s[2]:,}"
    fill = f"{qty} filled at ₩{value / qty:,.0f}" if qty else "not filled yet (booked on the next run)"
    return (f"🧪 **Order test** — order {oid} for 1 x {ticker}: {fill} ({status}).\n"
            f"Sleeve before: {sleeve(before)}\nSleeve after: {sleeve(after)}")


def format_log_failed(exc: BaseException) -> str:
    return f"⚠️ **Result log failed** — orders are fine, but results/ was not updated (is the CSV open in Excel?). `{exc}`"[:1900]
