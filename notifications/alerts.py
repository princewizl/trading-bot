"""
Email notification module.

Sends a rich HTML email for every high-confidence signal.
Uses Gmail SMTP with an App Password (not your Google account password).

Setup:
  1. Go to https://myaccount.google.com/apppasswords
  2. Create an App Password for "Mail" + "Windows Computer"
  3. Copy the 16-character password into your .env as EMAIL_PASSWORD
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

def send_signal_email(signal) -> bool:
    """Send a formatted signal email. Returns True on success."""
    if not _email_configured():
        logger.warning("Email not configured — check EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECIPIENT in .env")
        return False

    subject = _subject(signal)
    html    = _build_html(signal)
    plain   = _build_plain(signal)
    return _send(subject, html, plain)


def send_startup_email():
    """Send a short 'bot is running' notification."""
    if not _email_configured():
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = "Trading Bot Started"
    html = f"""
    <div style="font-family:sans-serif;padding:20px;background:#f5f5f5;">
      <h2 style="color:#1a73e8;">Trading Bot Started</h2>
      <p>Your Pocket Option signal bot is now running and scanning markets.</p>
      <p><strong>Started at:</strong> {now}</p>
      <p>You will receive an email each time a high-confidence signal is detected.</p>
    </div>"""
    _send(subject, html, f"Bot started at {now}. Scanning for high-confidence signals.")


def send_daily_summary(stats: dict):
    """Send end-of-day summary email."""
    if not _email_configured() or not stats:
        return
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"Daily Signal Summary — {date_str}"
    wr = stats.get("win_rate", 0)
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


# ── HTML builder ──────────────────────────────────────────────────────────

def _subject(signal) -> str:
    arrow = "↑ CALL" if signal.direction == "BUY" else "↓ PUT"
    name  = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    conf  = signal.confidence_label
    return f"[{conf}] {arrow} — {name} @ {signal.price:.5f}"


def _build_html(signal) -> str:
    name      = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    is_buy    = signal.direction == "BUY"
    dir_label = "CALL ↑" if is_buy else "PUT ↓"
    bg_color  = "#0f9d58" if is_buy else "#d93025"
    conf_bg   = {"HIGH": "#0f9d58", "MEDIUM": "#f4b400", "LOW": "#e57368"}.get(signal.confidence_label, "#aaa")
    ts        = signal.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    amount    = max(1.0, min(50.0, ACCOUNT_BALANCE * TRADE_AMOUNT_PCT))

    checks_passed_html = "".join(
        f'<li style="color:#0f9d58;">✔ {c}</li>' for c in signal.checks_passed
    )
    checks_failed_html = "".join(
        f'<li style="color:#999;">✘ {c}</li>' for c in signal.checks_failed
    )

    advice_html = signal.advice.replace("\n", "<br>")

    pattern_row = ""
    if signal.candlestick_pattern:
        pattern_row = f"""
        <tr>
          <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Candle Pattern</td>
          <td style="padding:10px 16px;border:1px solid #eee;color:#1a73e8;">{signal.candlestick_pattern}</td>
        </tr>"""

    # Upcoming news warning row
    upcoming = getattr(signal, "upcoming_news", [])
    news_row = ""
    if upcoming:
        next_ev = upcoming[0]
        news_row = f"""
        <tr>
          <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fff3cd;">⚠ Upcoming News</td>
          <td style="padding:10px 16px;border:1px solid #eee;color:#856404;">
            {next_ev['title']} ({next_ev['country']}) in {next_ev['minutes_away']} min
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:Arial,sans-serif;">

  <!-- HEADER -->
  <div style="background:{bg_color};padding:28px 20px;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:32px;">{dir_label}</h1>
    <h2 style="color:#fff;margin:6px 0 0;">{name}</h2>
    <p style="color:rgba(255,255,255,0.85);margin:4px 0 0;">{ts}</p>
  </div>

  <!-- CONFIDENCE BADGE -->
  <div style="text-align:center;padding:12px;background:#fff;">
    <span style="background:{conf_bg};color:#fff;padding:6px 20px;border-radius:20px;
                 font-size:14px;font-weight:bold;letter-spacing:1px;">
      {signal.confidence_label} CONFIDENCE — {signal.strength:.0%} filters passed
    </span>
  </div>

  <!-- SIGNAL STATS -->
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
        <td style="padding:10px 16px;border:1px solid #eee;font-weight:bold;background:#fafafa;">Suggested Amount</td>
        <td style="padding:10px 16px;border:1px solid #eee;">{CURRENCY}{amount:.2f} (2% of balance)</td>
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

  <!-- CONFLUENCE BREAKDOWN -->
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

  <!-- TRADING ADVICE -->
  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:16px 20px;border-bottom:1px solid #eee;background:#1a73e8;">
      <h3 style="margin:0;color:#fff;">How to Trade This Signal</h3>
    </div>
    <div style="padding:16px 20px;font-size:14px;line-height:1.8;color:#444;">
      {advice_html}
    </div>
  </div>

  <!-- DISCLAIMER -->
  <div style="max-width:600px;margin:16px auto 32px;padding:14px 20px;
              background:#fff3cd;border-radius:8px;border-left:4px solid #f4b400;
              font-size:12px;color:#856404;">
    <strong>Disclaimer:</strong> This is an automated signal based on technical analysis.
    It is NOT financial advice and does not guarantee profit. Trading binary options
    involves significant risk. Never invest money you cannot afford to lose.
  </div>

</body>
</html>"""


def _build_plain(signal) -> str:
    name   = PAIR_DISPLAY.get(signal.symbol, signal.symbol)
    ts     = signal.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    amount = max(1.0, min(50.0, ACCOUNT_BALANCE * TRADE_AMOUNT_PCT))
    lines  = [
        f"SIGNAL: {'CALL ↑' if signal.direction == 'BUY' else 'PUT ↓'}",
        f"Asset:  {name}",
        f"Price:  {signal.price:.5f}",
        f"Time:   {ts}",
        f"Confidence: {signal.confidence_label} ({signal.strength:.0%})",
        f"Expiry: {EXPIRY_MINUTES} min   Amount: {CURRENCY}{amount:.2f}",
        f"ADX: {signal.adx:.1f}   RSI: {signal.rsi:.1f}   H1 Trend: {signal.htf_trend}",
        "",
        "PASSED:", *[f"  ✔ {c}" for c in signal.checks_passed],
        "",
        "ADVICE:",
        signal.advice,
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
            "Gmail authentication failed. "
            "Make sure you are using an App Password, not your regular Gmail password. "
            "See: https://myaccount.google.com/apppasswords"
        )
        return False
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def _email_configured() -> bool:
    return bool(EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENT)
