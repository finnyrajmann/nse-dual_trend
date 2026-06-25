"""
NSE Dual Trend Swing Trader — DO Functions Entry Point
=======================================================
Uses only requests + standard library (no pip installs needed).
- Yahoo Finance API for price data
- GitHub REST API for reading/writing CSV data
- Gmail SMTP for notifications

Signal: Both 15-bar highest-high and lowest-low step lines
        flip to upward state on the same bar (fresh confluence).
No regime filter — the signal itself confirms the uptrend.
"""

import os
import csv
import smtplib
import time
import base64
import math
from io import StringIO
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
LOOKBACK      = 15
STOP_LOSS_PCT = 10.0
POSITION_SIZE = 10000
SLEEP         = 0.5


# ─────────────────────────────────────────────
# YAHOO FINANCE
# ─────────────────────────────────────────────
def fetch_ohlc(symbol, period='1y'):
    ticker = symbol.upper().strip()
    if not ticker.startswith("^"):
        ticker = ticker + ".NS"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params  = {'range': period, 'interval': '1d', 'events': 'history'}
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        r    = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        res  = data['chart']['result'][0]
        q    = res['indicators']['quote'][0]
        highs  = q['high']
        lows   = q['low']
        closes = q['close']
        # zip and filter out any None bars
        bars = [(h, l, c) for h, l, c in zip(highs, lows, closes)
                if h is not None and l is not None and c is not None]
        return bars
    except Exception as e:
        print(f"  ${ticker}: {e}")
        return None


# ─────────────────────────────────────────────
# DUAL TREND SIGNAL LOGIC
# ─────────────────────────────────────────────
def rolling_max(values, i, lookback):
    start = max(0, i - lookback + 1)
    return max(values[start:i+1])


def rolling_min(values, i, lookback):
    start = max(0, i - lookback + 1)
    return min(values[start:i+1])


def compute_dual_trend(bars, lookback=15):
    """
    Returns list of dicts per bar with:
      upper_line, lower_line, upper_state, lower_state,
      confluence, fresh_confluence
    """
    highs  = [b[0] for b in bars]
    lows   = [b[1] for b in bars]

    results    = []
    us         = 0   # upper_state — sticky
    ls         = 0   # lower_state — sticky

    for i in range(len(bars)):
        upper_now  = rolling_max(highs, i, lookback)
        lower_now  = rolling_min(lows,  i, lookback)

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

        confluence       = (us == 1) and (ls == 1)
        upper_just_flipped = (us == 1) and (prev_us != 1) and upper_broke_up
        lower_just_flipped = (ls == 1) and (prev_ls != 1) and lower_broke_up
        fresh_confluence = confluence and (upper_just_flipped or lower_just_flipped)

        results.append({
            'upper_line':      upper_now,
            'lower_line':      lower_now,
            'upper_state':     us,
            'lower_state':     ls,
            'confluence':      confluence,
            'fresh_confluence': fresh_confluence,
        })

    return results


def check_signal(symbol, bars, lookback=15):
    """
    Returns signal dict if today's bar shows fresh confluence, else None.
    """
    if not bars or len(bars) < lookback + 2:
        return None

    results = compute_dual_trend(bars, lookback)
    last    = results[-1]
    close   = bars[-1][2]
    stop    = round(close * (1 - STOP_LOSS_PCT / 100), 2)

    if last['fresh_confluence']:
        return {
            'symbol':     symbol,
            'price':      round(close, 2),
            'stop':       stop,
            'upper_line': round(last['upper_line'], 2),
            'lower_line': round(last['lower_line'], 2),
        }
    return None


# ─────────────────────────────────────────────
# GITHUB REST API
# ─────────────────────────────────────────────
def github_get(repo, path, pat):
    url     = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {'Authorization': f'token {pat}',
               'Accept': 'application/vnd.github.v3+json'}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data    = r.json()
    content = base64.b64decode(data['content']).decode('utf-8')
    return content, data['sha']


def github_put(repo, path, pat, content, sha, message):
    url     = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {'Authorization': f'token {pat}',
               'Accept': 'application/vnd.github.v3+json'}
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
def run_exit(positions, trade_log):
    exits         = []
    holds         = []
    new_positions = []

    for pos in positions:
        symbol      = pos['Symbol']
        entry_price = float(pos['EntryPrice'])
        quantity    = int(pos['Quantity'])
        entry_date  = datetime.strptime(pos['EntryDate'], '%Y-%m-%d')
        days_held   = (datetime.now() - entry_date).days
        track_type  = pos['TrackType']
        stop_price  = round(entry_price * (1 - STOP_LOSS_PCT / 100), 2)

        bars = fetch_ohlc(symbol)
        if bars is None or len(bars) < 2:
            new_positions.append(pos)
            continue

        close   = round(bars[-1][2], 2)
        pnl     = round((close - entry_price) * quantity, 2)
        pnl_pct = round((close - entry_price) / entry_price * 100, 2)

        # Compute current dual trend state for exit signal
        results     = compute_dual_trend(bars, LOOKBACK)
        last        = results[-1]
        upper_state = last['upper_state']
        lower_state = last['lower_state']

        exit_type   = None
        exit_reason = None

        # Exit when upper line flips red (upper_state turns -1) — trend lost
        if upper_state == -1 and lower_state == -1:
            exit_type   = 'SIGNAL'
            exit_reason = f"Both lines flipped bearish — trend reversal"
        elif close <= stop_price:
            exit_type   = 'STOP'
            exit_reason = f"Stop loss hit ({stop_price})"

        result = {
            'Symbol':     symbol,
            'TrackType':  track_type,
            'EntryPrice': entry_price,
            'EntryDate':  pos['EntryDate'],
            'Quantity':   quantity,
            'Price':      close,
            'PnL':        pnl,
            'PnL%':       pnl_pct,
            'DaysHeld':   days_held,
            'ExitType':   exit_type,
            'ExitReason': exit_reason,
            'UpperLine':  round(last['upper_line'], 2),
            'LowerLine':  round(last['lower_line'], 2),
            'Stop':       stop_price,
        }

        if exit_type:
            exits.append(result)
            trade_log.append({
                'Symbol':     symbol,
                'EntryDate':  pos['EntryDate'],
                'EntryPrice': entry_price,
                'Quantity':   quantity,
                'Capital':    round(entry_price * quantity, 2),
                'ExitDate':   datetime.now().strftime('%Y-%m-%d'),
                'ExitPrice':  close,
                'PnL':        pnl,
                'PnL%':       pnl_pct,
                'DaysHeld':   days_held,
                'ExitReason': exit_reason,
                'TrackType':  track_type,
            })
        else:
            holds.append(result)
            new_positions.append(pos)

        time.sleep(SLEEP)

    return exits, holds, new_positions, trade_log


# ─────────────────────────────────────────────
# ENTRY SCANNER
# ─────────────────────────────────────────────
def run_entry(watchlist, positions):
    open_symbols = {p['Symbol'].strip() for p in positions}
    new_entries  = []

    for row in watchlist:
        symbol = row['Symbol'].strip()
        if symbol in open_symbols:
            continue

        bars = fetch_ohlc(symbol)
        if bars is None:
            time.sleep(SLEEP)
            continue

        signal = check_signal(symbol, bars, LOOKBACK)
        if signal:
            quantity = max(1, int(POSITION_SIZE / signal['price']))
            positions.append({
                'Symbol':     symbol,
                'EntryDate':  datetime.now().strftime('%Y-%m-%d'),
                'EntryPrice': signal['price'],
                'Quantity':   quantity,
                'TrackType':  'Paper',
            })
            open_symbols.add(symbol)
            new_entries.append({
                'Symbol':    symbol,
                'Industry':  row.get('Industry', ''),
                'Price':     signal['price'],
                'UpperLine': signal['upper_line'],
                'LowerLine': signal['lower_line'],
                'Stop':      signal['stop'],
            })
            print(f"  Added {symbol} to positions as Paper "
                  f"(qty: {quantity} @ ₹{signal['price']})")

        time.sleep(SLEEP)

    return new_entries, positions


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────
def send_email(exits, entries, holds):
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

    html = f'''
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
    <h2 style="background:#2c3e50;color:#fff;padding:14px 18px;margin:0;border-radius:4px 4px 0 0;">
        📈 NSE Dual Trend Swing Trader — {today}
    </h2>
    '''

    # EXITS
    html += section_header(f'✅ Exits Today ({len(exits)})') if exits else section_header('✅ Exits: None today')
    if exits:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'P&L %', 'P&L ₹', 'Days', 'Reason']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in exits:
            icon = '🟢' if r['PnL'] >= 0 else '🔴'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">₹{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
                <td style="{td_style()}">{r['ExitReason']}</td>
            </tr>'''
        html += '</tbody></table>'

    # ENTRIES
    html += section_header(f'🔔 New Paper Entries ({len(entries)})') if entries else section_header('🔔 New Entries: None today')
    if entries:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Industry', 'Price ₹', 'Stop ₹', 'Upper Line ₹', 'Lower Line ₹']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for e in entries:
            html += f'''<tr>
                <td style="{td_style()}"><b>{e['Symbol']}</b></td>
                <td style="{td_style()}">{e['Industry']}</td>
                <td style="{td_style('right')}">₹{e['Price']}</td>
                <td style="{td_style('right')}">₹{e['Stop']}</td>
                <td style="{td_style('right')}">₹{e['UpperLine']}</td>
                <td style="{td_style('right')}">₹{e['LowerLine']}</td>
            </tr>'''
        html += '</tbody></table>'

    # OPEN POSITIONS
    if holds:
        total_pnl = sum(r['PnL'] for r in holds)
        pnl_color = '#27ae60' if total_pnl >= 0 else '#e74c3c'
        html += section_header(
            f'📋 Open Positions ({len(holds)}) &nbsp;|&nbsp; '
            f'Total P&L: <span style="color:{pnl_color}">₹{total_pnl:+.0f}</span>'
        )
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'Entry ₹', 'Price ₹', 'P&L %', 'P&L ₹', 'Days']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in holds:
            icon = '🟢' if r['PnL'] >= 0 else '🔴'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">₹{r['EntryPrice']:.2f}</td>
                <td style="{td_style('right')}">₹{r['Price']:.2f}</td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">₹{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
            </tr>'''
        html += '</tbody></table>'
    else:
        html += section_header('📋 Open Positions: None')

    # FOOTER
    html += f'''
    <p style="margin-top:24px;font-size:12px;color:#888;">
        <a href="https://github.com/{repo_name}/blob/main/data/dt_trade_log.csv" style="color:#2c3e50;">
            View trade log on GitHub
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

    try:
        # Load data from GitHub
        print("\n[1/5] Loading data from GitHub...")
        pos_content, pos_sha = github_get(repo_name, 'data/positions_dt.csv',  pat)
        log_content, log_sha = github_get(repo_name, 'data/dt_trade_log.csv',  pat)
        wl_content,  _       = github_get(repo_name, 'data/watchlist.csv',     pat)
        positions  = parse_csv(pos_content)
        trade_log  = parse_csv(log_content)
        watchlist  = parse_csv(wl_content)
        print(f"      {len(positions)} open positions | {len(watchlist)} watchlist stocks")

        # Exit monitor
        print("\n[2/5] Exit Monitor...")
        exits, holds, positions, trade_log = run_exit(positions, trade_log)
        print(f"      {len(exits)} exit(s) | {len(holds)} holding")

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

        github_put(repo_name, 'data/positions_dt.csv', pat,
                   to_csv(positions, pos_fields), pos_sha, commit_msg)
        github_put(repo_name, 'data/dt_trade_log.csv', pat,
                   to_csv(trade_log, log_fields), log_sha, commit_msg)

        # Send email
        print("\n[5/5] Sending email...")
        send_email(exits, entries, holds)

        print("\n  Done.\n")
        # Trigger DTDTF function
        dtdtf_url = os.environ.get('DTDTF_URL')
        if dtdtf_url:
            try:
                requests.post(dtdtf_url, json={}, timeout=10)
                print("  DTDTF function triggered.")
            except Exception as e:
                print(f"  DTDTF trigger failed: {e}")

        return {"statusCode": 200, "body": "Pipeline complete"}

    except Exception as e:
        import traceback
        print(f"\n  ERROR: {str(e)}")
        print(traceback.format_exc())
        return {"statusCode": 500, "body": str(e)}
