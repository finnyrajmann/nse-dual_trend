"""
NSE Dual Trend Swing Trader — DO Functions Entry Point
=======================================================
Uses only requests + standard library (no pip installs needed).
- Yahoo Finance API for price data (OHLC)
- GitHub REST API for reading/writing CSV data
- Gmail SMTP for notifications

Design summary (locked, Sep 2026 rewrite):
- Watchlist swapped to the shared screener output (structurally uptrend,
  Nifty500, healthcare-excluded) — same source as 200emabb / bb_ema_cross.
- Signal: both 15-bar highest-high and lowest-low step lines flip to
  upward state on the same bar (fresh confluence). No separate regime
  filter on the confluence signal itself.
- NEW daily EMA200 freshness gate on entry: price must be above EMA200
  TODAY (re-checked every run, not trusted from the screener's structural
  pass) — same discipline as 200emabb / bb_ema_cross.
- NEW stop exit: trailing stop, price <= 10% below the highest daily HIGH
  since entry (replaces the old fixed 10%-below-entry-price stop).
- Signal exit: both step lines flip bearish on the same bar (trend
  reversal) — no profit target; this system rides the open-ended trend,
  intentionally uncapped.
- If both the signal-reversal and trailing-stop conditions fire on the
  same day, logged as a distinct STOP_BOTH exit reason.
- Below-EMA200 warning: an open position whose price falls below EMA200
  while the dual-trend confluence is still bullish is NOT force-exited —
  only flagged in the email (mirrors 200emabb / bb_ema_cross).
- Hit/miss split: PnL% > 3.0 -> hit, PnL% <= 3.0 -> miss, written to two
  separate trade logs instead of one combined log.
- Existing open positions carry forward into the new logic as-is (no
  special migration) — a bulk closure on the first run under the new
  trailing-stop/signal rules is expected and fine.
- File naming: system code as SUFFIX everywhere —
  watchlist_dt.csv, positions_dt.csv, trade_log_hit_dt.csv,
  trade_log_miss_dt.csv. The old combined dt_trade_log.csv is retired
  (archived separately, not read by this script).
"""

import os
import csv
import smtplib
import time
import base64
from io import StringIO
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SYSTEM_CODE       = "dt"

LOOKBACK          = 15
TRAIL_STOP_PCT    = 10.0
EMA_LONG          = 200
POSITION_SIZE     = 10000
SLEEP             = 0.5
HIT_THRESHOLD_PCT = 3.0   # PnL% strictly greater than this -> hit, else -> miss

DATA_PERIOD       = "1y"  # sufficient for the 15-bar step lines and EMA200
                           # (EMA200 here is a daily freshness gate only —
                           # the deep 300-close structural check lives in
                           # the separate watchlist screener)


# ─────────────────────────────────────────────
# YAHOO FINANCE
# ─────────────────────────────────────────────
def fetch_price_bars(symbol, period=DATA_PERIOD):
    """
    Fetch daily OHLC bars for a symbol.
    Returns a list of dicts: {'date', 'high', 'low', 'close'}
    ordered oldest -> newest. Returns None on failure.
    """
    ticker = symbol.upper().strip()
    if not ticker.startswith("^"):
        ticker = ticker + ".NS"

    params = {
        'range':    period,
        'interval': '1d',
        'events':   'history',
    }
    headers = {'User-Agent': 'Mozilla/5.0'}

    for host in ['query1', 'query2']:
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
            r = requests.get(url, params=params, headers=headers, timeout=15)
            data = r.json()
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            highs  = quote['high']
            lows   = quote['low']
            closes = quote['close']

            bars = []
            for i, ts in enumerate(timestamps):
                c = closes[i]
                h = highs[i]
                l = lows[i]
                if c is None or h is None or l is None:
                    continue
                bars.append({
                    'date':  datetime.utcfromtimestamp(ts).date(),
                    'high':  h,
                    'low':   l,
                    'close': c,
                })
            if bars:
                return bars
        except Exception:
            continue
    return None


def calc_ema(values, period):
    """Calculate EMA over a list of closes (oldest -> newest)."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 2)


def high_since(bars, entry_dt):
    """Highest daily HIGH from entry_dt (inclusive) to the most recent bar."""
    relevant = [b['high'] for b in bars if b['date'] >= entry_dt]
    if not relevant:
        return bars[-1]['high'] if bars else None
    return max(relevant)


# ─────────────────────────────────────────────
# DUAL TREND SIGNAL LOGIC
# ─────────────────────────────────────────────
def rolling_max(values, i, lookback):
    start = max(0, i - lookback + 1)
    return max(values[start:i+1])


def rolling_min(values, i, lookback):
    start = max(0, i - lookback + 1)
    return min(values[start:i+1])


def compute_dual_trend(bars, lookback=LOOKBACK):
    """
    Returns list of dicts per bar with:
      upper_line, lower_line, upper_state, lower_state,
      confluence, fresh_confluence
    """
    highs = [b['high'] for b in bars]
    lows  = [b['low']  for b in bars]

    results = []
    us = 0   # upper_state — sticky
    ls = 0   # lower_state — sticky

    for i in range(len(bars)):
        upper_now = rolling_max(highs, i, lookback)
        lower_now = rolling_min(lows,  i, lookback)

        if i == 0:
            upper_prev = upper_now
            lower_prev = lower_now
        else:
            upper_prev = rolling_max(highs, i-1, lookback)
            lower_prev = rolling_min(lows,  i-1, lookback)

        upper_broke_up   = upper_now > upper_prev
        upper_broke_down = upper_now < upper_prev
        lower_broke_up   = lower_now > lower_prev
        lower_broke_down = lower_now < lower_prev

        prev_us = us
        prev_ls = ls

        if upper_broke_up:
            us = 1
        elif upper_broke_down:
            us = -1

        if lower_broke_up:
            ls = 1
        elif lower_broke_down:
            ls = -1

        confluence          = (us == 1) and (ls == 1)
        upper_just_flipped  = (us == 1) and (prev_us != 1) and upper_broke_up
        lower_just_flipped  = (ls == 1) and (prev_ls != 1) and lower_broke_up
        fresh_confluence    = confluence and (upper_just_flipped or lower_just_flipped)

        results.append({
            'upper_line':       upper_now,
            'lower_line':       lower_now,
            'upper_state':      us,
            'lower_state':      ls,
            'confluence':       confluence,
            'fresh_confluence': fresh_confluence,
        })

    return results


def get_indicators(symbol, period=DATA_PERIOD):
    """Get price + EMA200 + dual-trend state + raw bars for a symbol."""
    bars = fetch_price_bars(symbol, period)
    if not bars or len(bars) < LOOKBACK + 2:
        return None

    closes = [b['close'] for b in bars]
    price  = round(closes[-1], 2)
    ema200 = calc_ema(closes, EMA_LONG)

    dt_results = compute_dual_trend(bars, LOOKBACK)
    last       = dt_results[-1]

    return {
        'price':            price,
        'ema200':           ema200,
        'bars':             bars,
        'upper_line':       round(last['upper_line'], 2),
        'lower_line':       round(last['lower_line'], 2),
        'upper_state':      last['upper_state'],
        'lower_state':      last['lower_state'],
        'confluence':       last['confluence'],
        'fresh_confluence': last['fresh_confluence'],
    }


# ─────────────────────────────────────────────
# GITHUB REST API
# ─────────────────────────────────────────────
def github_get(repo, path, pat):
    """Read a file from GitHub. Returns (content, sha)."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data    = r.json()
    content = base64.b64decode(data['content']).decode('utf-8')
    return content, data['sha']


def github_put(repo, path, pat, content, sha, message):
    """Write a file to GitHub."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
    }
    payload = {
        'message': message,
        'content': base64.b64encode(content.encode('utf-8')).decode('utf-8'),
        'sha':     sha,
    }
    r = requests.put(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    return True


def parse_csv(content):
    reader = csv.DictReader(StringIO(content))
    return list(reader)


def to_csv(rows, fieldnames):
    out    = StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# ─────────────────────────────────────────────
# EXIT MONITOR
# ─────────────────────────────────────────────
def run_exit(positions, hit_log, miss_log):
    """
    Check every open position for signal-reversal / trailing-stop exit.
    Returns (exits, holds, warnings, remaining_positions, hit_log, miss_log)
    """
    exits         = []
    holds         = []
    warnings      = []   # below-EMA200 but still confluence-bullish — flag only
    new_positions = []
    hit_log       = list(hit_log)
    miss_log      = list(miss_log)

    for pos in positions:
        symbol      = pos['Symbol']
        entry_price = float(pos['EntryPrice'])
        quantity    = int(pos['Quantity'])
        entry_date  = datetime.strptime(pos['EntryDate'], '%Y-%m-%d')
        track_type  = pos['TrackType']
        capital     = round(entry_price * quantity, 2)
        days_held   = (datetime.now() - entry_date).days

        ind = get_indicators(symbol)
        if ind is None:
            new_positions.append(pos)
            time.sleep(SLEEP)
            continue

        price = ind['price']

        hi_since = high_since(ind['bars'], entry_date.date())
        trail_stop_price = round(hi_since * (1 - TRAIL_STOP_PCT / 100), 2) if hi_since else None

        signal_reversed = (ind['upper_state'] == -1 and ind['lower_state'] == -1)
        trail_stop      = trail_stop_price is not None and price <= trail_stop_price

        exit_type   = None
        exit_reason = None

        if signal_reversed and trail_stop:
            exit_type   = 'STOP_BOTH'
            exit_reason = (f"Both lines flipped bearish AND trailing stop hit "
                            f"({trail_stop_price}, high since entry {hi_since})")
        elif signal_reversed:
            exit_type   = 'SIGNAL'
            exit_reason = "Both lines flipped bearish — trend reversal"
        elif trail_stop:
            exit_type   = 'STOP_TRAIL'
            exit_reason = f"Trailing stop hit ({trail_stop_price}, high since entry {hi_since})"

        pnl     = round((price - entry_price) * quantity, 2)
        pnl_pct = round((price - entry_price) / entry_price * 100, 2)

        if exit_type:
            record = {
                'Symbol':     symbol,
                'EntryDate':  pos['EntryDate'],
                'EntryPrice': entry_price,
                'Quantity':   quantity,
                'Capital':    capital,
                'ExitDate':   datetime.now().strftime('%Y-%m-%d'),
                'ExitPrice':  price,
                'PnL':        pnl,
                'PnL%':       pnl_pct,
                'DaysHeld':   days_held,
                'ExitReason': exit_reason,
                'TrackType':  track_type,
            }
            exits.append(record)
            if pnl_pct > HIT_THRESHOLD_PCT:
                hit_log.append(record)
            else:
                miss_log.append(record)
        else:
            new_positions.append(pos)
            holds.append({
                'Symbol':     symbol,
                'EntryPrice': entry_price,
                'Price':      price,
                'PnL':        pnl,
                'PnL%':       pnl_pct,
                'DaysHeld':   days_held,
            })

            if (ind['ema200'] is not None and price < ind['ema200']
                    and ind['confluence']):
                warnings.append({
                    'Symbol':  symbol,
                    'Price':   price,
                    'EMA200':  ind['ema200'],
                    'PnL%':    pnl_pct,
                })

        time.sleep(SLEEP)

    return exits, holds, warnings, new_positions, hit_log, miss_log


# ─────────────────────────────────────────────
# ENTRY SCANNER
# ─────────────────────────────────────────────
def run_entry(watchlist, positions):
    """
    Watchlist is pre-vetted for STRUCTURAL uptrend shape (EMA200
    3-checkpoint slope check) by the separate periodic screener. Whether
    price is currently above EMA200 is re-checked fresh every run here.
    Entry signal: fresh confluence (both step lines flip bullish together)
    AND price above EMA200 today.
    """
    open_symbols = {p['Symbol'].strip() for p in positions}
    new_entries  = []

    for row in watchlist:
        symbol = row['Symbol'].strip()
        if symbol in open_symbols:
            continue

        ind = get_indicators(symbol)
        if ind is None:
            time.sleep(SLEEP)
            continue

        # Daily freshness gate: price must be above EMA200 TODAY (the
        # screener only guarantees the structural slope shape).
        if ind['ema200'] is None or ind['price'] <= ind['ema200']:
            time.sleep(SLEEP)
            continue

        if ind['fresh_confluence']:
            quantity   = max(1, int(POSITION_SIZE / ind['price']))
            entry_date = datetime.now().strftime('%Y-%m-%d')

            positions.append({
                'Symbol':     symbol,
                'EntryDate':  entry_date,
                'EntryPrice': ind['price'],
                'Quantity':   quantity,
                'TrackType':  'Paper',
            })
            open_symbols.add(symbol)

            new_entries.append({
                'Symbol':      symbol,
                'Industry':    row.get('Industry', ''),
                'Price':       ind['price'],
                'UpperLine':   ind['upper_line'],
                'LowerLine':   ind['lower_line'],
                'InitialStop': round(ind['price'] * (1 - TRAIL_STOP_PCT / 100), 2),
            })
            print(f"  Added {symbol} to positions as Paper "
                  f"(qty: {quantity} @ Rs.{ind['price']})")

        time.sleep(SLEEP)

    return new_entries, positions


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────
def send_email(exits, entries, holds, warnings, alltime_pnl, alltime_count,
               hit_count, miss_count):
    sender    = os.environ.get('GMAIL_SENDER')
    password  = os.environ.get('GMAIL_APP_PASSWORD')
    recipient = os.environ.get('GMAIL_RECIPIENT')
    repo_name = os.environ.get('GITHUB_REPO')
    today     = datetime.now().strftime('%d %b %Y')
    subject   = f"NSE Dual Trend — {today} | {len(entries)} new | {len(holds)} open"

    def table_style():
        return 'border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;'

    def th_style():
        return 'background:#2c3e50;color:#fff;padding:8px 12px;text-align:left;'

    def td_style(align='left'):
        return f'padding:7px 12px;border-bottom:1px solid #eee;text-align:{align};'

    def section_header(title):
        return f'<h3 style="color:#2c3e50;margin:24px 0 8px 0;">{title}</h3>'

    hits   = [e for e in exits if e['PnL%'] > HIT_THRESHOLD_PCT]
    misses = [e for e in exits if e['PnL%'] <= HIT_THRESHOLD_PCT]

    html = f'''
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
    <h2 style="background:#2c3e50;color:#fff;padding:14px 18px;margin:0;border-radius:4px 4px 0 0;">
        📈 NSE Dual Trend Swing Trader — {today}
    </h2>
    '''

    # EXITS
    html += section_header(
        f'✅ Exits Today ({len(exits)}) &mdash; {len(hits)} hit / {len(misses)} miss'
    ) if exits else section_header('✅ Exits: None today')
    if exits:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'P&L %', 'P&L Rs', 'Days', 'Reason']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in exits:
            icon = '🟢' if r['PnL%'] > HIT_THRESHOLD_PCT else '🔴'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">Rs.{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
                <td style="{td_style()}">{r['ExitReason']}</td>
            </tr>'''
        html += '</tbody></table>'

    # ENTRIES
    html += section_header(f'🔔 New Paper Entries ({len(entries)})') \
        if entries else section_header('🔔 New Entries: None today')
    if entries:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Industry', 'Price Rs', 'Initial Stop Rs', 'Upper Line Rs', 'Lower Line Rs']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for e in entries:
            html += f'''<tr>
                <td style="{td_style()}"><b>{e['Symbol']}</b></td>
                <td style="{td_style()}">{e['Industry']}</td>
                <td style="{td_style('right')}">Rs.{e['Price']}</td>
                <td style="{td_style('right')}">Rs.{e['InitialStop']}</td>
                <td style="{td_style('right')}">Rs.{e['UpperLine']}</td>
                <td style="{td_style('right')}">Rs.{e['LowerLine']}</td>
            </tr>'''
        html += '</tbody></table>'

    # OPEN POSITIONS
    if holds:
        total_pnl = sum(r['PnL'] for r in holds)
        pnl_color = '#27ae60' if total_pnl >= 0 else '#e74c3c'
        html += section_header(
            f'📋 Open Positions ({len(holds)}) &nbsp;|&nbsp; '
            f'Total P&L: <span style="color:{pnl_color}">Rs.{total_pnl:+.0f}</span>'
        )
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'Entry Rs', 'Price Rs', 'P&L %', 'P&L Rs', 'Days']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in holds:
            icon = '🟢' if r['PnL'] >= 0 else '🔴'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">Rs.{r['EntryPrice']:.2f}</td>
                <td style="{td_style('right')}">Rs.{r['Price']:.2f}</td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">Rs.{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
            </tr>'''
        html += '</tbody></table>'
    else:
        html += section_header('📋 Open Positions: None')

    # BELOW-EMA200 WARNING (open positions only)
    if warnings:
        html += section_header(f'⚠️ Below EMA200, Still Confluence-Bullish ({len(warnings)})')
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Price Rs', 'EMA200 Rs', 'P&L %']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for w in warnings:
            html += f'''<tr>
                <td style="{td_style()}"><b>{w['Symbol']}</b></td>
                <td style="{td_style('right')}">Rs.{w['Price']:.2f}</td>
                <td style="{td_style('right')}">Rs.{w['EMA200']:.2f}</td>
                <td style="{td_style('right')}">{w['PnL%']:+.2f}%</td>
            </tr>'''
        html += '</tbody></table>'

    # CUMULATIVE TRADE LOG P&L
    at_color = '#27ae60' if alltime_pnl >= 0 else '#e74c3c'
    hit_rate = f'{(hit_count / alltime_count * 100):.0f}%' if alltime_count else 'N/A'
    html += section_header('📊 All-Time Trade Log')
    html += f'''
    <table style="{table_style()}"><tbody>
        <tr>
            <td style="{td_style()}">Closed trades</td>
            <td style="{td_style('right')}">{alltime_count} ({hit_count} hit / {miss_count} miss, {hit_rate} hit rate)</td>
        </tr>
        <tr>
            <td style="{td_style()}">Cumulative P&amp;L</td>
            <td style="{td_style('right')}"><span style="color:{at_color}"><b>Rs.{alltime_pnl:+,.0f}</b></span></td>
        </tr>
    </tbody></table>
    '''

    # FOOTER
    html += f'''
    <p style="margin-top:24px;font-size:12px;color:#888;">
        <a href="https://github.com/{repo_name}/blob/master/data/trade_log_hit_{SYSTEM_CODE}.csv" style="color:#2c3e50;">
            View hit log
        </a> &nbsp;|&nbsp;
        <a href="https://github.com/{repo_name}/blob/master/data/trade_log_miss_{SYSTEM_CODE}.csv" style="color:#2c3e50;">
            View miss log
        </a><br>
        — NSE Dual Trend Trader (automated)
    </p>
    </div>
    '''

    msg = MIMEMultipart()
    msg['From']    = sender
    msg['To']      = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    print(f"  Email sent to {recipient}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main(args):
    print("\n" + "="*50)
    print("  NSE DUAL TREND TRADER — DO Functions Run")
    print("="*50)

    pat       = os.environ.get('GITHUB_PAT')
    repo_name = os.environ.get('GITHUB_REPO')

    pos_path      = f'data/positions_{SYSTEM_CODE}.csv'
    hit_log_path  = f'data/trade_log_hit_{SYSTEM_CODE}.csv'
    miss_log_path = f'data/trade_log_miss_{SYSTEM_CODE}.csv'
    wl_path       = f'data/watchlist_{SYSTEM_CODE}.csv'

    try:
        # Load data from GitHub
        print("\n[1/5] Loading data from GitHub...")
        pos_content, pos_sha       = github_get(repo_name, pos_path, pat)
        hitlog_content, hit_sha    = github_get(repo_name, hit_log_path, pat)
        misslog_content, miss_sha  = github_get(repo_name, miss_log_path, pat)
        wl_content, _              = github_get(repo_name, wl_path, pat)

        positions = parse_csv(pos_content)
        hit_log   = parse_csv(hitlog_content)
        miss_log  = parse_csv(misslog_content)
        watchlist = parse_csv(wl_content)
        print(f"      {len(positions)} open positions | {len(watchlist)} watchlist stocks")

        # Exit monitor
        print("\n[2/5] Exit Monitor...")
        exits, holds, warnings, positions, hit_log, miss_log = run_exit(positions, hit_log, miss_log)
        print(f"      {len(exits)} exit(s) | {len(holds)} holding | {len(warnings)} below-EMA200 warning(s)")

        # Entry scanner
        print("\n[3/5] Entry Scanner...")
        entries, positions = run_entry(watchlist, positions)
        print(f"      {len(entries)} new signal(s)")

        # Sync to GitHub
        print("\n[4/5] Syncing to GitHub...")
        commit_msg = f"Auto-update — {datetime.now().strftime('%Y-%m-%d')}"

        pos_fields = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 'TrackType']
        log_fields = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 'Capital',
                      'ExitDate', 'ExitPrice', 'PnL', 'PnL%', 'DaysHeld',
                      'ExitReason', 'TrackType']

        github_put(repo_name, pos_path, pat,
                   to_csv(positions, pos_fields), pos_sha, commit_msg)
        github_put(repo_name, hit_log_path, pat,
                   to_csv(hit_log, log_fields), hit_sha, commit_msg)
        github_put(repo_name, miss_log_path, pat,
                   to_csv(miss_log, log_fields), miss_sha, commit_msg)

        # Cumulative trade-log P&L
        alltime_pnl = (sum(float(r['PnL']) for r in hit_log) +
                       sum(float(r['PnL']) for r in miss_log))
        alltime_count = len(hit_log) + len(miss_log)

        # Send email
        print("\n[5/5] Sending email...")
        send_email(exits, entries, holds, warnings, alltime_pnl, alltime_count,
                   len(hit_log), len(miss_log))

        print("\n  Done.\n")
        return {"statusCode": 200, "body": "Pipeline complete"}

    except Exception as e:
        import traceback
        print(f"\n  ERROR: {str(e)}")
        print(traceback.format_exc())
        return {"statusCode": 500, "body": str(e)}
