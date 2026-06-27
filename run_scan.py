"""
Single-scan entry point for GitHub Actions.

GitHub Actions calls this script on a schedule (every 5 minutes).
It does exactly one full scan cycle — all pairs, all filters — then exits.
Emails are sent for any qualifying signals.

Usage:
    python run_scan.py           # normal scan
    python run_scan.py --dry-run # scan without sending emails (test mode)
"""

import logging
import sys
from pathlib import Path

# ── Logging to stdout so GitHub Actions captures it ──────────────────────
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
from strategy.trend_following import TrendFollowingStrategy
from notifications.alerts import send_signal_email
from main import filter_correlated


def run():
    dry_run  = "--dry-run" in sys.argv
    strategy = TrendFollowingStrategy()

    active = active_sessions_now()
    logger.info(f"Scan started — active sessions: {active or ['off-peak']}")
    if dry_run:
        logger.info("DRY RUN MODE — emails will NOT be sent")

    candidates = []

    for symbol in config.TRADING_PAIRS:
        name = config.PAIR_DISPLAY.get(symbol, symbol)

        # ── Session filter ────────────────────────────────────────────
        session_ok, session_info = is_session_active(symbol)
        if not session_ok:
            wait = minutes_to_next_session(symbol)
            logger.info(f"SKIP {name}: off-peak session — next in {wait} min")
            continue

        # ── News calendar filter ──────────────────────────────────────
        blocked, news_reason = is_news_blocked(symbol)
        if blocked:
            logger.info(f"SKIP {name}: {news_reason}")
            continue

        # ── Fetch data + strategy ─────────────────────────────────────
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
                f"CANDIDATE {name} {sig.direction} "
                f"{sig.confidence_label} {sig.strength:.0%} "
                f"ADX={sig.adx:.1f} RSI={sig.rsi:.1f} "
                f"MACD={sig.macd_hist:+.6f} BB={sig.bb_width*100:.3f}%"
            )
        else:
            logger.debug(f"NO SIGNAL {name}: {sig.advice}")

    # ── Correlation filter ────────────────────────────────────────────
    kept = filter_correlated(candidates)
    dropped_count = len(candidates) - len(kept)
    if dropped_count:
        logger.info(f"Correlation filter removed {dropped_count} redundant signal(s)")

    # ── Send emails ───────────────────────────────────────────────────
    sent_count = 0
    for sig in kept:
        name = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
        if dry_run:
            logger.info(f"[DRY RUN] Would email: {name} {sig.direction} {sig.confidence_label}")
            continue
        ok = send_signal_email(sig)
        if ok:
            sent_count += 1
            logger.info(f"Email sent: {name} {sig.direction} {sig.confidence_label}")
        else:
            logger.error(f"Email FAILED: {name} — check EMAIL_* secrets in GitHub")

    logger.info(
        f"Scan complete — "
        f"candidates={len(candidates)} kept={len(kept)} "
        f"emails_sent={sent_count}"
    )


if __name__ == "__main__":
    try:
        run()
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Scan crashed: {e}")
        sys.exit(1)
