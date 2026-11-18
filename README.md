# NSE Dual Trend Swing Trader

Automated daily paper-trading system for NSE equities. It runs as a DigitalOcean Function, reads its state from a GitHub repo, and emails a daily summary through Gmail.

## How it works

Each run does five things:

1. **Load state** from the GitHub repo: open positions, hit and miss trade logs, the watchlist, and the entry snapshot log.
2. **Exit monitor** checks every open position against the trailing stop and the signal reversal.
3. **Entry scanner** checks each watchlist stock for a fresh signal and a price above EMA200 today.
4. **Sync** writes the updated positions, logs, and snapshots back to GitHub.
5. **Email** sends the day's exits, new entries, open positions, below-EMA200 warnings, and all-time P&L.

## Signal logic

- **Dual trend lines:** the 15-bar highest high (upper line) and the 15-bar lowest low (lower line). Each line is sticky: it flips to bullish when it breaks up and to bearish when it breaks down.
- **Entry:** both lines flip bullish on the same bar (fresh confluence), and the close is above EMA200 on that day. The EMA200 check is re-run every day, not trusted from the screener.
- **Signal exit:** both lines flip bearish on the same bar. There is no profit target. The system rides the trend until it reverses.
- **Trailing stop exit:** the stop is 3% below the highest candle body top since entry. The body top is `max(open, close)` of each daily candle, so wicks are ignored. The exit fires when the daily close is at or below the stop.
- **STOP_BOTH:** if the signal reversal and the trailing stop fire on the same day, the exit is logged as `STOP_BOTH`.
- **Below-EMA200 warning:** an open position that closes below EMA200 while the confluence is still bullish is flagged in the email. It is not force-exited.

## Trading calendar

The run uses an explicit holiday list, `data/nse_holidays.csv`, read from the GitHub repo. It has three columns: `Date`, `Type`, and `Description`. `Type` is `HOLIDAY` for a market holiday or `COVERS_THROUGH` for the date the list is confirmed to. A day is a trading session if it is Mon–Fri and not a listed holiday.

- **Non-session day:** the run exits early. On a weekday holiday it sends a short holiday email and touches no positions or logs. Weekends exit silently.
- **Stale data:** a stock whose newest bar is older than the last completed session is not given a new entry. It is listed in the email. Open positions are still evaluated on the data available.
- **Calendar warnings:** the email flags a calendar that is missing, not confirmed through today, or expiring within 14 days.
- **Force run:** invoke with `{"force": true}` to run on a non-session day.

## Configuration

Set in `__main__.py`:

| Constant | Value | Meaning |
|---|---|---|
| `LOOKBACK` | 15 | Bars for the upper and lower step lines |
| `TRAIL_STOP_PCT` | 3.0 | Trailing stop percent below the body-top peak |
| `EMA_LONG` | 200 | Daily freshness gate for entry |
| `POSITION_SIZE` | 10000 | Rupees per paper position |
| `HIT_THRESHOLD_PCT` | 3.0 | Exit PnL above this is a hit, otherwise a miss |
| `DATA_PERIOD` | `1y` | Yahoo Finance history window |
| `SLEEP` | 0.5 | Pause between API calls, in seconds |

## Environment variables

| Variable | Purpose |
|---|---|
| `GITHUB_PAT` | GitHub token with read and write access to the data repo |
| `GITHUB_REPO` | Repo in `owner/name` form |
| `GMAIL_SENDER` | Gmail address that sends the email |
| `GMAIL_APP_PASSWORD` | Gmail app password for that address |
| `GMAIL_RECIPIENT` | Comma-separated recipient list |

## Repo data files

All files live under `data/` and use the `_dt` system suffix.

| File | Role |
|---|---|
| `nse_holidays.csv` | Trading calendar, read from the repo at each run |
| `watchlist_dt.csv` | Input: stocks to scan, with an `Industry` column. Produced by the shared screener. |
| `positions_dt.csv` | Open positions |
| `trade_log_hit_dt.csv` | Closed trades with PnL% above the hit threshold |
| `trade_log_miss_dt.csv` | Closed trades with PnL% at or below the hit threshold |
| `entry_snapshot_dt.csv` | Indicator values recorded at each entry |

The old combined `dt_trade_log.csv` is retired and not read by this script.

## Deployment

1. Deploy `__main__.py` as a DigitalOcean Function with the entry point `main`.
2. Set the five environment variables above.
3. Make sure the `data/` files exist in the repo, with headers, before the first run. Commit `data/nse_holidays.csv` to the repo as well, since the function reads it from GitHub, not from the local folder.
4. Trigger on a schedule after the NSE close.

Dependencies: `requests`. The rest is the standard library.

## Notes

- Existing open positions carry forward. The first run under the 3% body-top stop may close several positions at once. This is expected.
- The data source is Yahoo Finance with the `.NS` suffix for NSE symbols. Each symbol is fetched from `query1` and falls back to `query2`.
- If a symbol's data cannot be fetched, the position is kept as it is for that run.
