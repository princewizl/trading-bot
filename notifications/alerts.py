"""
Email notification module.

Sends:
  1. Signal email — when a high-confidence signal is detected
     (shows "Trade placed on Deriv" banner when auto-trading is enabled)
  2. Trade result email — after contract expires: WIN or LOSS with PnL + balance
  3. Startup email — bot is running confirmation
  4. Daily summary email — end-of-day stats

Setup:
  Use EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECIPIENT in .env
"""

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    CURRENCY,
    EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT,
    SMTP_HOST, SMTP_PORT,
    PAIR_DISPLAY, EXPIRY_MINUTES, ACCOUNT_BALANCE, TRADE_AMOUNT_PCT,
)

logger = logging.getLogger(__name__)


# ── Public API ────────────────────────────────────────────────────────────

def send_signal_email(signal, auto_trading: bool = False, amount: float = None) -> bool:
    """
    Send a formatted signal email.
    auto_trading=True adds a "Trade placed on Deriv — awaiting result" banner.
    Returns True on success.
    """
    if not _email_configured():
        logger.warning("Email not configured — check EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECIPIENT in .env")
        return False

    if amount is None:
        amount = max(1.0, min(50.0, ACCOUNT_BALANCE * TRADE_AMOUNT_PCT))

    subject = _signal_subject(signal, auto_trading)
    html    = _build_signal_html(signal, auto_trading, amount)
    plain   = _build_signal_plain(signal, auto_trading, amount)
    return _send(subject, html, plain)


def send_trade_result_email(signal, result: dict, amount: float) -> bool:
    """
    Send a trade result email after Deriv contract settles.
    result keys: outcome (WIN/LOSS), profit, balance, currency, entry_spot, exit_spot, stake, payout
    """
    if not _email_configured():
        return False

    subject = _result_subject(signal, result)
    html    = _build_result_html(signal, result, amount)
    plain   = _build_result_plain(signal, result)
    return _send(subject, html, plain)


def send_startup_email():
    if not _email_configured():
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = "Trading Bot Started"
    html = f"""
    <div style="font-family:sans-serif;padding:20px;background:#f5f5f5;">
      <h2 style="color:#1a73e8;">Trading Bot Started</h2>
      <p>Your Forex signal bot is now running and scanning markets.</p>
      <p><strong>Started at:</strong> {now}</p>
      <p>You will receive an email each time a high-confidence signal is detected.</p>
    </div>"""
    _send(subject, html, f"Bot started at {now}. Scanning for high-confidence signals.")


def send_daily_summary(stats: dict):
    if not _email_configured() or not stats:
        return
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject  = f"Daily Signal Summary — {date_str}"
    wr  = stats.get("win_rate", 0)
    pnl = stats.get("net_pnl", 0)
    color = "#0f9d58" if pnl >= 0 else "#d93025"
    html = f"""
    <div style="font-family:sans-serif;padding:20px;background:#f5f5f5;">
      <h2 style="color:#1a73e8;">Daily Summary — {date_str}</h2>
      <table style="border-collapse:collapse;width:300px;">
        <tr><td style="padding:8px;border:1px solid #ddd;"><b>Signals Sent</b></td>
            <td style="padding:8px;border:1px solid #ddd;">{stats.get('total_trades', 0)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;"><b>Win Rate</b></td>
            <td style="padding:8px;border:1px solid #ddd;">{wr:.1%}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;"><b>Net PnL</b></td>
            <td style="padding:8px;border:1px solid #ddd;color:{color};">{CURRENCY}{pnl:+.2f}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;"><b>Balance</b></td>
            <td style="padding:8px;border:1px solid #ddd;">{CURRENCY}{stats.get('balance', 0):.2f}</td></tr>
      </table>
    </div>"""
    _send(subject, html, f"Daily summary: {stats.get('total_trades',0)} signals, win rate {wr:.1%}")


# ── Signal email builder ───────────────────────────────────────────────────

def _signal_subject(signal, auto_trading: bool) -> str:
    arrow = "↑ CALL" if signal.direction == "BUY" else "↓ PUT"
    name  = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    tag   = " [AUTO-TRADE]" if auto_trading else ""
    return f"[{signal.confidence_label}] {arrow} — {name} @ {signal.price:.5f}{tag}"


def _build_signal_html(signal, auto_trading: bool, amount: float) -> str:
    name      = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    is_buy    = signal.direction == "BUY"
    dir_label = "CALL ↑" if is_buy else "PUT ↓"
    bg_color  = "#0f9d58" if is_buy else "#d93025"
    conf_bg   = {"HIGH": "#0f9d58", "MEDIUM": "#f4b400", "LOW": "#e57368"}.get(signal.confidence_label, "#aaa")
    ts        = signal.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

    checks_passed_html = "".join(
        f'<li style="color:#0f9d58;">&#10004; {c}</li>' for c in signal.checks_passed
    )
    checks_failed_html = "".join(
        f'<li style="color:#999;">&#10008; {c}</li>' for c in signal.checks_failed
    )

    advice_html = signal.advice.replace("\n", "<br>")

    pattern_row = ""
    if signal.candlestick_pattern:
        pattern_row = f"""
        <tr>
          <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Candle Pattern</td>
          <td style="padding:10px 16px;border:1px solid #eee;color:#1a73e8;">{signal.candlestick_pattern}</td>
        </tr>"""

    upcoming = getattr(signal, "upcoming_news", [])
    news_row = ""
    if upcoming:
        ev = upcoming[0]
        news_row = f"""
        <tr>
          <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fff3cd;">&#9888; Upcoming News</td>
          <td style="padding:10px 16px;border:1px solid #eee;color:#856404;">
            {ev['title']} ({ev['country']}) in {ev['minutes_away']} min
          </td>
        </tr>"""

    # Auto-trade banner (shown only when Deriv auto-trading is active)
    auto_banner = ""
    if auto_trading:
        auto_banner = f"""
  <div style="max-width:600px;margin:16px auto;background:#e8f5e9;border-radius:8px;
              border-left:5px solid #0f9d58;padding:16px 20px;">
    <p style="margin:0;font-size:15px;color:#1b5e20;">
      <strong>&#9889; Trade Placed on Deriv Automatically</strong><br>
      <span style="font-size:13px;">
        Stake: <strong>{signal.currency if hasattr(signal,'currency') else 'USD'} {amount:.2f}</strong> &nbsp;|&nbsp;
        Expiry: <strong>{EXPIRY_MINUTES} minute(s)</strong> &nbsp;|&nbsp;
        You will receive a result email when the contract expires.
      </span>
    </p>
  </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:Arial,sans-serif;">

  <div style="background:{bg_color};padding:28px 20px;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:32px;">{dir_label}</h1>
    <h2 style="color:#fff;margin:6px 0 0;">{name}</h2>
    <p style="color:rgba(255,255,255,0.85);margin:4px 0 0;">{ts}</p>
  </div>

  <div style="text-align:center;padding:12px;background:#fff;">
    <span style="background:{conf_bg};color:#fff;padding:6px 20px;border-radius:20px;
                 font-size:14px;font-weight:bold;letter-spacing:1px;">
      {signal.confidence_label} CONFIDENCE &#8212; {signal.strength:.0%} filters passed
    </span>
  </div>

  {auto_banner}

  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:16px 20px;border-bottom:1px solid #eee;">
      <h3 style="margin:0;color:#333;">Signal Details</h3>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Asset</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{name}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Direction</td>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;color:{bg_color};">{dir_label}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Entry Price</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{signal.price:.5f}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Expiry</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{EXPIRY_MINUTES} minute(s)</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Stake</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{amount:.2f} (2% of balance)</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">H1 Trend</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{signal.htf_trend}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">ADX</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{signal.adx:.1f}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">RSI</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{signal.rsi:.1f}</td>
      </tr>
      {pattern_row}
      {news_row}
    </table>
  </div>

  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:16px 20px;border-bottom:1px solid #eee;">
      <h3 style="margin:0;color:#333;">Confluence Checklist</h3>
    </div>
    <div style="padding:16px 20px;">
      <ul style="margin:0;padding-left:20px;line-height:2;">
        {checks_passed_html}
        {checks_failed_html}
      </ul>
    </div>
  </div>

  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:16px 20px;border-bottom:1px solid #eee;background:#1a73e8;">
      <h3 style="margin:0;color:#fff;">How to Trade This Signal</h3>
    </div>
    <div style="padding:16px 20px;font-size:14px;line-height:1.8;color:#444;">
      {advice_html}
    </div>
  </div>

  <div style="max-width:600px;margin:16px auto 32px;padding:14px 20px;
              background:#fff3cd;border-radius:8px;border-left:4px solid #f4b400;
              font-size:12px;color:#856404;">
    <strong>Disclaimer:</strong> This is an automated signal based on technical analysis.
    It is NOT financial advice and does not guarantee profit. Trading involves significant
    risk. Never invest money you cannot afford to lose.
  </div>

</body>
</html>"""


def _build_signal_plain(signal, auto_trading: bool, amount: float) -> str:
    name  = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    ts    = signal.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"SIGNAL: {'CALL' if signal.direction == 'BUY' else 'PUT'}",
        f"Asset:  {name}",
        f"Price:  {signal.price:.5f}",
        f"Time:   {ts}",
        f"Confidence: {signal.confidence_label} ({signal.strength:.0%})",
        f"Expiry: {EXPIRY_MINUTES} min   Stake: {amount:.2f} (2% of balance)",
        f"ADX: {signal.adx:.1f}   RSI: {signal.rsi:.1f}   H1 Trend: {signal.htf_trend}",
    ]
    if auto_trading:
        lines += ["", "*** TRADE PLACED ON DERIV AUTOMATICALLY ***", "Result email will follow after contract expires."]
    lines += ["", "PASSED:", *[f"  + {c}" for c in signal.checks_passed],
              "", "ADVICE:", signal.advice]
    return "\n".join(lines)


# ── Trade result email builder ─────────────────────────────────────────────

def _result_subject(signal, result: dict) -> str:
    outcome  = result["outcome"]
    profit   = result["profit"]
    currency = result["currency"]
    name     = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    emoji    = "WIN" if outcome == "WIN" else "LOSS"
    sign     = "+" if profit >= 0 else ""
    return f"[TRADE {emoji}] {name} {signal.direction} | {currency} {sign}{profit:.2f}"


def _build_result_html(signal, result: dict, stake: float) -> str:
    outcome   = result["outcome"]
    profit    = result["profit"]
    balance   = result["balance"]
    currency  = result["currency"]
    payout    = result.get("payout", 0)
    entry     = result.get("entry_spot", signal.price)
    exit_spot = result.get("exit_spot", 0)
    cid       = result.get("contract_id", "")

    name    = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    ts      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    is_win  = outcome == "WIN"

    bg_color    = "#0f9d58" if is_win else "#d93025"
    result_icon = "&#9989;" if is_win else "&#10060;"  # ✅ or ❌
    profit_sign = "+" if profit >= 0 else ""
    dir_label   = "CALL ↑" if signal.direction == "BUY" else "PUT ↓"

    price_move = ""
    if exit_spot and entry:
        move   = exit_spot - entry
        move_p = (move / entry) * 100 if entry else 0
        color  = "#0f9d58" if move > 0 else "#d93025"
        price_move = f"""
        <tr>
          <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Price Move</td>
          <td style="padding:10px 16px;border:1px solid #eee;color:{color};">
            {entry:.5f} &#8594; {exit_spot:.5f} ({move_p:+.4f}%)
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:Arial,sans-serif;">

  <!-- RESULT HEADER -->
  <div style="background:{bg_color};padding:28px 20px;text-align:center;">
    <div style="font-size:48px;">{result_icon}</div>
    <h1 style="color:#fff;margin:8px 0 0;font-size:36px;">TRADE {outcome}</h1>
    <h2 style="color:#fff;margin:6px 0 0;">{name} &mdash; {dir_label}</h2>
    <p style="color:rgba(255,255,255,0.85);margin:4px 0 0;">{ts}</p>
  </div>

  <!-- PnL SUMMARY BANNER -->
  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);padding:20px;text-align:center;">
    <div style="display:inline-block;margin:0 20px;">
      <div style="font-size:13px;color:#666;margin-bottom:4px;">Profit / Loss</div>
      <div style="font-size:32px;font-weight:bold;color:{bg_color};">
        {currency} {profit_sign}{profit:.2f}
      </div>
    </div>
    <div style="display:inline-block;margin:0 20px;border-left:2px solid #eee;padding-left:24px;">
      <div style="font-size:13px;color:#666;margin-bottom:4px;">New Balance</div>
      <div style="font-size:32px;font-weight:bold;color:#333;">
        {currency} {balance:.2f}
      </div>
    </div>
  </div>

  <!-- TRADE DETAILS -->
  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:16px 20px;border-bottom:1px solid #eee;">
      <h3 style="margin:0;color:#333;">Trade Details</h3>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Asset</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{name}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Direction</td>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;color:{bg_color};">{dir_label}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Stake</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{currency} {stake:.2f}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Payout Received</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{currency} {payout:.2f}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Profit / Loss</td>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;color:{bg_color};">
          {currency} {profit_sign}{profit:.2f}
        </td>
      </tr>
      {price_move}
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Signal Confidence</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{signal.confidence_label} ({signal.strength:.0%})</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">ADX / RSI</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{signal.adx:.1f} / {signal.rsi:.1f}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Contract ID</td>
        <td style="padding:10px 16px;border:1px solid #eee;font-size:12px;color:#777;">{cid}</td>
      </tr>
    </table>
  </div>

  <!-- BALANCE CONTEXT -->
  <div style="max-width:600px;margin:16px auto;background:#f8f9fa;border-radius:8px;
              border-left:4px solid {'#0f9d58' if is_win else '#d93025'};padding:16px 20px;">
    <p style="margin:0;font-size:14px;color:#333;">
      Account balance updated to <strong>{currency} {balance:.2f}</strong>
      after this trade. Next stake (2%): <strong>{currency} {balance * 0.02:.2f}</strong>
    </p>
  </div>

  <div style="max-width:600px;margin:16px auto 32px;padding:14px 20px;
              background:#fff3cd;border-radius:8px;border-left:4px solid #f4b400;
              font-size:12px;color:#856404;">
    <strong>Disclaimer:</strong> Past results do not guarantee future performance.
    Trading involves significant risk of loss.
  </div>

</body>
</html>"""


def _build_result_plain(signal, result: dict) -> str:
    outcome  = result["outcome"]
    profit   = result["profit"]
    balance  = result["balance"]
    currency = result["currency"]
    name     = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    sign     = "+" if profit >= 0 else ""
    lines = [
        f"TRADE {outcome}",
        f"Asset:   {name}  ({signal.direction})",
        f"Profit:  {currency} {sign}{profit:.2f}",
        f"Balance: {currency} {balance:.2f}",
        f"Next stake (2%): {currency} {balance * 0.02:.2f}",
        f"Confidence: {signal.confidence_label} ({signal.strength:.0%})",
        f"Contract ID: {result.get('contract_id', 'n/a')}",
    ]
    return "\n".join(lines)


# ── SMTP helper ────────────────────────────────────────────────────────────

def _send(subject: str, html: str, plain: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_RECIPIENT

        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())

        logger.info(f"Email sent: {subject}")
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. "
            "Check EMAIL_SENDER and EMAIL_PASSWORD in .env"
        )
        return False
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def _email_configured() -> bool:
    return bool(EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENT)
