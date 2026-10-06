"""One-off capital deposit into the bot sleeve: python deposit.py AMOUNT --confirm [YYYY-MM-DD]
Adds AMOUNT to bot_cash AND to the kill-switch baselines (previous-run equity, peak), so the deposit is neither
profit (daily-loss check) nor a fake new peak (drawdown check). Records the event in state.json and a row in
results/daily_results.csv. Also raise budget_krw in config.toml by AMOUNT: that is the P&L base."""
import json
import os
import shutil
import sys
import tomllib
from datetime import date, datetime

import data
import results


def apply_deposit(state: dict, amount: int, day: str) -> dict:
    deposits = state.get("deposits", [])
    if amount <= 0:
        raise ValueError("amount must be positive")
    if any(d["date"] == day and d["amount"] == amount for d in deposits):
        raise ValueError(f"deposit of {amount:,} on {day} is already recorded")
    out = {**state, "bot_cash": round(state["bot_cash"] + amount, 2), "deposits": deposits + [{"date": day, "amount": amount}]}
    for k in ("equity", "peak"):
        if out.get(k) is not None:
            out[k] += amount
    return out


if __name__ == "__main__":
    data.utf8_stdio()
    if "--confirm" not in sys.argv or len(sys.argv) < 3:
        raise SystemExit("usage: python deposit.py AMOUNT --confirm [YYYY-MM-DD]")
    amount = int(sys.argv[1].replace(",", "").replace("_", ""))
    day = next((a for a in sys.argv[2:] if a[:2] == "20"), date.today().isoformat())
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    state = json.load(open("state.json", encoding="utf-8"))
    os.makedirs("logs", exist_ok=True)
    backup = f"logs/state.json.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy("state.json", backup)
    new = apply_deposit(state, amount, day)
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(new, f)
    row = results.day_row(day, equity=new["equity"], cash=new["bot_cash"], prev_equity=new["equity"], budget=cfg["budget_krw"],
                          peak=new["peak"], notes=f"deposit +{amount:,} (capital, not P&L)")
    print(f"backup: {backup}")
    print(f"bot_cash {state['bot_cash']:,.0f} -> {new['bot_cash']:,.0f}; equity {state['equity']:,} -> {new['equity']:,}; "
          f"peak {state['peak']:,} -> {new['peak']:,}")
    print("daily_results.csv:", "deposit row added" if results.log_daily(row) else "deposit row already there")
