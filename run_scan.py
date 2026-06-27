"""
Single-scan entry point — triggered by cron-job.org every 5 minutes.

Full flow per run:
  1. All filters: session → news calendar → 10-confluence strategy → correlation
  2. If signal found AND Deriv configured → place trade automatically
  3. Wait for contract expiry (5 min)
  4. Check WIN or LOSS from Deriv API
  5. Send email: signal details + trade placed + result (profit/loss + balance)

If DERIV_API_TOKEN is not set → signal-only mode (email the signal, no auto-trade).

Usage:
    python run_scan.py            # live mode
    python run_scan.py --dry-run  # scan only, no trades, no emails
"""

import logging
import sys
import time
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
from strategy.trend_following import TrendFollowingStrategy, Signal
from notifications.alerts import send_signal_email, send_trade_result_email
from main import filter_correlated


def deriv_is_configured() -> bool:
    return bool(config.DERIV_API_TOKEN)


def get_trade_amount() -> float:
    """2% of account balance, clamped to platform limits."""
    return round(max(config.MIN_TRADE_AMOUNT, min(config.MAX_TRADE_AMOUNT, config.ACCOUNT_BALANCE * config.TRADE_AMOUNT_PCT)), 2)


def place_and_await(signal: Signal, dry_run: bool) -> dict | None:
    """
    Connect to Deriv, place the trade, wait for expiry, return result.
    Returns None if Deriv not configured or trade fails.
    """
    if dry_run:
        logger.info(f"[DRY RUN] Would place {signal.direction} on {signal.symbol}")
        return None

    if not deriv_is_configured():
        logger.info("Deriv not configured — signal-only mode")
        return None

    deriv_symbol = config.DERIV_SYMBOLS.get(signal.symbol)
    if not deriv_symbol:
        logger.warning(f"No Deriv symbol mapping for {signal.symbol}")
        return None

    from execution.deriv import DerivClient
    client = DerivClient(
        api_token=config.DERIV_API_TOKEN,
        app_id=config.DERIV_APP_ID,
        demo=config.DERIV_DEMO,
    )

    try:
        if not client.connect():
            logger.error("Could not connect to Deriv — skipping auto-trade")
            return None

        amount = get_trade_amount()
        logger.info(
            f"Placing trade: {signal.symbol} {signal.direction} "
            f"{client.currency} {amount:.2f} "
            f"({'DEMO' if config.DERIV_DEMO else 'LIVE'})"
        )

        trade = client.place_trade(
            symbol=deriv_symbol,
            direction=signal.direction,
            amount=amount,
            duration_minutes=config.DERIV_EXPIRY_MINUTES,
        )

        if trade is None:
            logger.error("Trade placement failed")
            return None

        # Wait for expiry and get result
        result = client.wait_for_result(trade)
        return result

    finally:
        client.disconnect()


def run():
    dry_run  = "--dry-run" in sys.argv
    strategy = TrendFollowingStrategy()

    active = active_sessions_now()
    mode   = "DEMO" if config.DERIV_DEMO else "LIVE"
    auto   = "AUTO-TRADE" if deriv_is_configured() else "SIGNAL-ONLY"
    logger.info(f"Scan started | Sessions: {active or ['off-peak']} | Mode: {auto} {mode if deriv_is_configured() else ''}")

    if dry_run:
        logger.info("DRY RUN — no emails, no trades")

    # ── 1. Gather candidates (all filters) ───────────────────────────────
    candidates: list[Signal] = []

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
            logger.info(
                f"SIGNAL {name} {sig.direction} {sig.confidence_label} "
                f"{sig.strength:.0%} | ADX={sig.adx:.1f} RSI={sig.rsi:.1f} "
                f"MACD={sig.macd_hist:+.6f} BB={sig.bb_width*100:.3f}%"
            )
        else:
            logger.debug(f"NO SIGNAL {name}: {sig.advice}")

    # ── 2. Correlation filter ─────────────────────────────────────────────
    kept   = filter_correlated(candidates)
    dropped = len(candidates) - len(kept)
    if dropped:
        logger.info(f"Correlation filter removed {dropped} redundant signal(s)")

    if not kept:
        logger.info(f"Scan complete — no actionable signals this cycle")
        return

    # ── 3. For each signal: send alert, place trade, await result, send result ──
    for sig in kept:
        name = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
        amount = get_trade_amount()

        logger.info(f"Processing signal: {name} {sig.direction} {sig.confidence_label}")

        # Send signal email (with "trade being placed" note if auto mode)
        if not dry_run:
            send_signal_email(sig, auto_trading=deriv_is_configured(), amount=amount)

        # Place trade and wait for result
        result = place_and_await(sig, dry_run)

        if result:
            outcome  = result["outcome"]
            profit   = result["profit"]
            balance  = result["balance"]
            currency = result["currency"]

            logger.info(
                f"{'✅' if outcome == 'WIN' else '❌'} {outcome}: {name} {sig.direction} | "
                f"Profit: {currency} {profit:+.2f} | Balance: {currency} {balance:.2f}"
            )

            if not dry_run:
                send_trade_result_email(sig, result, amount)

        elif deriv_is_configured() and not dry_run:
            logger.warning(f"Could not get result for {name} — check Deriv dashboard")

    logger.info(f"Scan complete — {len(kept)} signal(s) processed")


if __name__ == "__main__":
    try:
        run()
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Scan crashed: {e}")
        sys.exit(1)
