"""All Discord-facing text. Corporate Mundo voice; logs elsewhere stay plain."""


def format_backtest_summary(results: dict) -> str:
    """results: {period: {ticker: {"strategy": {...}, "bh": {...}}}} as built in run.py."""
    import statistics

    def med(xs):
        xs = [x for x in xs if x == x]
        return statistics.median(xs) if xs else float("nan")

    lines = ["📊 **Quarterly Synergy Report** — Mundo Strategy Division deliver result, very leverage."]
    for name, rs in results.items():
        if not rs:
            lines.append(f"• {name}: no data. Department of Data is on lunch, please circle back.")
            continue
        s = med(r["strategy"]["sharpe"] for r in rs.values())
        b = med(r["bh"]["sharpe"] for r in rs.values())
        verdict = "Strategy exceed expectation, bonus for everyone!" if s > b else "Strategy underperform, Mundo schedule urgent alignment meeting."
        lines.append(f"• **{name}**: strategy Sharpe {s:.2f} vs B&H {b:.2f} (median, {len(rs)} tickers). {verdict}")
    return "\n".join(lines)


def format_error(exc: BaseException) -> str:
    return f"🚨 **Incident Escalation** — Mundo system have critical blocker, no bandwidth to proceed.\n`{type(exc).__name__}: {exc}`"[:1900]


def format_order_filled(ticker: str, name: str, side: str, qty: int, price: float) -> str:
    verb = "acquire" if side.lower() == "buy" else "divest"
    return f"💼 **Deal Closed** — Mundo {verb} {qty:,} share of {name} ({ticker}) at ₩{price:,.0f}. Win-win, very stakeholder value."


def format_order_submitted(ticker: str, name: str, side: str, qty: int) -> str:
    return f"📨 **Action Item Submitted** — Mundo request {side} of {qty:,} share of {name} ({ticker}). Pending approval from Market Department."


def format_order_rejected(ticker: str, name: str, side: str, qty: int, reason: str) -> str:
    return f"❌ **Request Denied** — Market Department say no to {side} {qty:,} {name} ({ticker}). Reason: {reason[:300]}. Mundo will take offline."


def format_order_pending(ticker: str, name: str, side: str, qty: int) -> str:
    return f"⏳ **Still In Pipeline** — {side} {qty:,} {name} ({ticker}) not yet fill. Mundo circle back later."


def format_skipped_zero_shares(ticker: str, price: float) -> str:
    return f"⚠️ **Budget Constraint** — Mundo cannot afford even 1 share of {ticker} (₩{price:,.0f}) in this slice. Item descoped, please advise."


def format_kill_switch(reason: str) -> str:
    return f"🛑 **Emergency Freeze** — Mundo halt all new order, no more budget for risk. Reason: {reason[:500]}"


def format_reconcile_mismatch(diffs: list[str]) -> str:
    body = "\n".join(f"• {d}" for d in diffs)
    return f"🔍 **Audit Finding** — Mundo book not match broker book. Broker is source of truth, Mundo adjust.\n{body}"[:1900]


def format_eod_summary(equity: int, cash: int, positions: dict, n_orders: int, dry_run: bool = False) -> str:
    held = ", ".join(f"{p['name']} x{p['qty']}" for p in positions.values()) or "none"
    tag = " (dry run, no order was send)" if dry_run else ""
    return (f"📈 **End-of-Day Wrap-Up**{tag} — Mundo total asset ₩{equity:,}, cash ₩{cash:,}. "
            f"{n_orders} order today. Holdings: {held}. Please align on next steps.")[:1900]


def format_test_alert() -> str:
    return "🔔 **Webhook Onboarding Complete** — Mundo notification pipeline is live. Please acknowledge receipt, per my last message."
