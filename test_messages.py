import messages

S = {"strategy": {"sharpe": 1.0}, "bh": {"sharpe": 0.5}}


def test_formatters():
    outs = [
        messages.format_backtest_summary({"in-sample": {"005930": S}, "out-of-sample": {}}),
        messages.format_error(ValueError("x" * 5000)),
        messages.format_order_filled("005930", "Samsung Electronics", "buy", 10, 70000),
        messages.format_order_filled("005930", "Samsung Electronics", "sell", 10, 70000),
        messages.format_test_alert(),
        messages.format_outside_window('15:00', '09:00', '09:30'),
        messages.format_order_submitted("005930", "Samsung", "buy", 3),
        messages.format_order_rejected("005930", "Samsung", "buy", 3, "x" * 5000),
        messages.format_order_pending("005930", "Samsung", "sell", 3),
        messages.format_kill_switch("y" * 5000),
        messages.format_skipped_zero_shares("005930", 276000),
        messages.format_reconcile_mismatch(["d" * 300] * 20),
        messages.format_eod_summary(10**7, 5 * 10**6, {"005930": {"name": "Samsung", "qty": 3}}, 2, 9 * 10**6),
        messages.format_eod_summary(10**7, 5 * 10**6, {}, 0, 10**7, dry_run=True),
    ]
    for o in outs:
        assert isinstance(o, str) and 0 < len(o) < 2000


def test_eod_shows_sleeve_pnl():
    o = messages.format_eod_summary(137_200, 20_000, {}, 0, 140_000)
    assert "₩137,200" in o and "-2,800" in o and "-2.0%" in o and "₩140,000" in o
