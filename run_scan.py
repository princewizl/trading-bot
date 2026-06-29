"""
Single-scan entry point — triggered by cron-job.org every 5 minutes.

Flow:
  1. Session + news + strategy + correlation filters → signals
  2. Connect to IQ Option ONCE for the whole scan (one login per run)
  3. Per signal: place trade → send signal email → wait expiry → result email → log
  4. Disconnect

If IQ_EMAIL/IQ_PASSWORD not set, or connection fails → signal-only mode.

Usage:
    python run_scan.py            # live mode
    python run_scan.py --dry-run  # scan only, no trades, no emails
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

import config
from data.fetcher import fetch_ohlcv
from data.indicators import add_all_indicators
from data.session import is_session_active, active_sessions_now, minutes_to_next_session
from data.calendar import is_news_blocked, next_news_events
from data.trade_journal import log_trade
from strategy.trend_following import TrendFollowingStrategy, Signal
from notifications.alerts import send_signal_email, send_trade_result_email
from notifications.weekly_report import should_send_weekly, send_weekly_report
from main import filter_correlated


def iq_is_configured() -> bool:
    return bool(config.IQ_EMAIL and config.IQ_PASSWORD)


def get_trade_amount(live_balance: float = None) -> float:
    """2% of balance, clamped between MIN and MAX trade amounts."""
    balance = live_balance if live_balance and live_balance > 0 else config.ACCOUNT_BALANCE
    return round(max(config.MIN_TRADE_AMOUNT, min(config.MAX_TRADE_AMOUNT, balance * config.TRADE_AMOUNT_PCT)), 2)


def run():
    dry_run  = "--dry-run" in sys.argv
    strategy = TrendFollowingStrategy()

    active = active_sessions_now()
    mode   = "DEMO" if config.IQ_DEMO else "LIVE"
    auto   = "AUTO-TRADE" if iq_is_configured() else "SIGNAL-ONLY"
    logger.info(f"Scan started | Sessions: {active or ['off-peak']} | Mode: {auto} {mode if iq_is_configured() else ''}")

    if dry_run:
        logger.info("DRY RUN — no emails, no trades")

    # Weekly performance report (Sundays 20:00 UTC)
    if not dry_run and should_send_weekly():
        logger.info("Sunday 20:00 UTC — sending weekly performance report")
        send_weekly_report()

    # ── 1. Scan all pairs ─────────────────────────────────────────────────
    candidates: list[Signal] = []
    session_map: dict[str, str] = {}

    for symbol in config.TRADING_PAIRS:
        name = config.PAIR_DISPLAY.get(symbol, symbol)

        session_ok, session_info = is_session_active(symbol)
        if not session_ok:
            wait = minutes_to_next_session(symbol)
            logger.info(f"SKIP {name}: off-peak — {wait} min to next session")
            continue

        blocked, news_reason = is_news_blocked(symbol)
        if blocked:
            logger.info(f"SKIP {name}: {news_reason}")
            continue

        df_m5 = fetch_ohlcv(symbol, interval=config.SIGNAL_INTERVAL, period=config.SIGNAL_PERIOD)
        if df_m5 is None or len(df_m5) < 60:
            logger.warning(f"SKIP {name}: insufficient data")
            continue
        df_m5 = add_all_indicators(df_m5)

        df_h1 = fetch_ohlcv(symbol, interval=config.TREND_INTERVAL, period=config.TREND_PERIOD)
        sig   = strategy.analyze(symbol, df_m5, htf_df=df_h1)

        if sig.is_actionable:
            sig.upcoming_news = next_news_events(symbol, look_ahead_hours=2)
            candidates.append(sig)
            session_map[symbol] = session_info
            logger.info(
                f"SIGNAL {name} {sig.direction} {sig.confidence_label} "
                f"{sig.strength:.0%} | ADX={sig.adx:.1f} RSI={sig.rsi:.1f} "
                f"MACD={sig.macd_hist:+.6f} BB={sig.bb_width*100:.3f}%"
            )

    # ── 2. Correlation filter ─────────────────────────────────────────────
    kept    = filter_correlated(candidates)
    dropped = len(candidates) - len(kept)
    if dropped:
        logger.info(f"Correlation filter removed {dropped} redundant signal(s)")

    if not kept:
        logger.info("Scan complete — no actionable signals this cycle")
        return

    # ── 3. Connect to IQ Option ONCE for all signals ──────────────────────
    iq_client    = None
    live_balance = None

    if iq_is_configured() and not dry_run:
        from execution.iqoption import IQClient
        client = IQClient(
            email=config.IQ_EMAIL,
            password=config.IQ_PASSWORD,
            demo=config.IQ_DEMO,
        )
        if client.connect():
            iq_client    = client
            live_balance = client.balance
        else:
            logger.warning("IQ Option unavailable — emailing signals only this scan")

    # ── 4. Process each signal ────────────────────────────────────────────
    try:
        for sig in kept:
            name       = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
            iq_symbol  = config.IQ_SYMBOLS.get(sig.symbol)
            can_trade  = iq_client is not None and iq_symbol is not None
            amount     = get_trade_amount(live_balance)

            logger.info(f"Processing signal: {name} {sig.direction} {sig.confidence_label}")

            result       = None
            trade_placed = False

            if can_trade:
                logger.info(f"Placing trade: {name} {sig.direction} USD {amount:.2f}")
                trade = iq_client.place_trade(
                    symbol=iq_symbol,
                    direction=sig.direction,
                    amount=amount,
                    duration_minutes=config.EXPIRY_MINUTES,
                )

                if trade:
                    trade_placed = True
                    # Email goes out immediately after trade is confirmed placed
                    if not dry_run:
                        send_signal_email(sig, auto_trading=True, amount=amount)

                    result = iq_client.wait_for_result(trade)
                    if result:
                        result["actual_stake"] = amount

            # No trade placed → signal-only email
            if not dry_run and not trade_placed:
                send_signal_email(sig, auto_trading=False, amount=amount)

            # Result email + journal
            if result:
                outcome  = result["outcome"]
                profit   = result["profit"]
                balance  = result["balance"]
                currency = result["currency"]
                logger.info(
                    f"{'WIN' if outcome == 'WIN' else 'LOSS'}: {name} {sig.direction} | "
                    f"Profit: {currency} {profit:+.2f} | Balance: {currency} {balance:.2f}"
                )
                if not dry_run:
                    send_trade_result_email(sig, result, amount)
                    log_trade(sig, result, amount, session=session_map.get(sig.symbol, ""))

    finally:
        if iq_client:
            iq_client.disconnect()

    logger.info(f"Scan complete — {len(kept)} signal(s) processed")


if __name__ == "__main__":
    try:
        run()
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Scan crashed: {e}")
        sys.exit(1)
