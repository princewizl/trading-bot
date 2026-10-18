"""
One place the bot service sends every notification through.

Push (PWA) is the primary channel. Email is optional (EMAIL_ENABLED=true) and
reuses notifications/alerts.py unchanged.
"""

import logging
import os

import config
from notifications import push

logger = logging.getLogger(__name__)

EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"


def signal(sig, placed: bool, amount: float, reason: str = "") -> None:
    name = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
    if placed:
        title = f"{'▲' if sig.direction == 'BUY' else '▼'} {name} {sig.direction} — trade placed"
        body = f"Stake ${amount:.2f} · {sig.strength:.0%} ({sig.confidence_label}) · ADX {sig.adx:.0f} · RSI {sig.rsi:.0f}"
    else:
        title = f"{name} {sig.direction} — signal only"
        body = f"{reason or 'Not auto-traded'} · {sig.strength:.0%} · ADX {sig.adx:.0f} · RSI {sig.rsi:.0f}"
    push.send(title, body, tag=f"signal-{sig.symbol}")
    if EMAIL_ENABLED:
        from notifications.alerts import send_signal_email
        _email(lambda: send_signal_email(sig, auto_trading=placed, amount=amount))


def result(sig, res: dict, stake: float) -> None:
    name = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
    icon = {"WIN": "✅", "LOSS": "❌", "TIE": "➖"}.get(res["outcome"], "⚠️")
    title = f"{icon} {res['outcome']} — {name} {sig.direction} {res['profit']:+.2f}"
    body = f"Balance ${res['balance']:.2f}" + ("" if res["outcome"] != "UNKNOWN" else " · result not reported by IQ Option")
    push.send(title, body, tag=f"result-{res['contract_id']}")
    if EMAIL_ENABLED:
        from notifications.alerts import send_trade_result_email
        _email(lambda: send_trade_result_email(sig, res, stake))


def alert(title: str, body: str, tag: str = "alert") -> None:
    """Operational alerts: circuit breaker, IQ Option down/up, bot offline/online."""
    logger.warning(f"ALERT: {title} — {body}")
    push.send(f"⚠️ {title}", body, tag=tag, urgent=True)
    if EMAIL_ENABLED:
        def send():
            from notifications.alerts import _send
            _send(f"[Trading Bot] {title}", f"<p>{body}</p>", body)
        _email(send)


def weekly(rows: list[dict]) -> None:
    """Weekly summary from journal-shaped rows (data.db.journal_rows)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week = [r for r in rows if r["timestamp"] >= cutoff and r["outcome"] in ("WIN", "LOSS")]
    if not week:
        return
    wins = sum(r["outcome"] == "WIN" for r in week)
    pnl = sum(float(r["profit"] or 0) for r in week)
    push.send("📊 Weekly report", f"{len(week)} trades · {wins / len(week):.0%} win rate · P&L {pnl:+.2f}", tag="weekly")
    if EMAIL_ENABLED:
        from notifications.weekly_report import send_weekly_report
        _email(lambda: send_weekly_report(all_trades=rows))


def _email(fn) -> None:
    try:
        fn()
    except Exception as e:
        logger.error(f"Email failed: {e}")
