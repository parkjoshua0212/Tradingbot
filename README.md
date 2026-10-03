# Korean stock trading bot — Phase 1

Daily OHLCV backfill (pykrx → SQLite), event-loop backtester, SMA-crossover sanity strategy. No live orders.

## Run
```
pip install pandas pykrx
python test_backtest.py     # self-check
python run.py               # backfill + backtest (in-sample / out-of-sample tables)
python run.py --offline     # backtest from the existing DB only
python check_data.py        # compare closes by hand vs Naver; flags >±30% daily jumps
```
Edit `config.toml` for tickers, dates, `split_date`, costs, and SMA windows.
Optional `.env` (see `.env.example`): `KRX_ID`/`KRX_PW`, only needed if you set `ADJUSTED = False` in `data.py`.
Changing `ADJUSTED` means deleting `ohlcv.db` and re-backfilling (adjusted and raw rows must not mix).

## Design notes
- Signal uses closes up to day *t*; the order fills at day *t+1*'s open (no look-ahead).
- Costs: commission both sides, sell-side transaction tax, slippage against you on each fill. **Check the rates in `config.toml` against your broker and current KRX tax.**
- Backfill refetches the whole window and upserts on `(ticker, date)`: re-runs never duplicate and fill gaps. Zero-price (halted) days are skipped.
- Prices are split/capital-change adjusted (Naver, `adjusted=True`), not dividend-adjusted: returns exclude dividends.
- The SMA parameters are fixed, not fitted, so "in-sample" vs "out-of-sample" is a stability check across regimes, not a train/test fit. The strategy runs once over the full history (positions carry across the split); each period is scored from the prior close's equity, and buy-and-hold is bought at the period's first bar with the same capital and costs.
- Long-only, all-in, integer shares, each ticker backtested separately. Open positions are marked to the last close (strategy and buy-and-hold alike; buy-and-hold starts at the strategy's first possible fill bar).
- Secrets: env vars only (`.env.example`); nothing uses them yet.
