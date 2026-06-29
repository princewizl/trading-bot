"""
Single-scan entry point — triggered by cron-job.org every 5 minutes.

Flow:
  1. Session + news + strategy + correlation filters → signals
  2. Connect to IQ Option ONCE
  3. Phase A — Place ALL trades immediately (no waiting between them)
  4. Phase B — Wait ONCE for the last expiry (all trades expire together)
  5. Phase C — Check results for every trade, then email + log
  6. Disconnect

If IQ_EMAIL/IQ_PASSWORD not set, or connection fails → signal-only mode.

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
from data.trade_journal import log_trade
from strategy.trend_following import TrendFollowingStrategy, Signal
from notifications.alerts import send_signal_email, send_trade_result_email
from notifications.weekly_report import should_send_weekly, send_weekly_report
from main import filter_correlated


def iq_is_configured() -> bool:
    return bool(config.IQ_EMAIL and config.IQ_PASSWORD)


def get_trade_amount(live_balance: float = None) -> float:
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

    # ── 3. Connect to IQ Option ONCE ─────────────────────────────────────
    iq_client    = None
    live_balance = None

    if iq_is_configured() and not dry_run:
        from execution.iqoption import IQClient
        client = IQClient(email=config.IQ_EMAIL, password=config.IQ_PASSWORD, demo=config.IQ_DEMO)
        if client.connect():
            iq_client    = client
            live_balance = client.balance
        else:
            logger.warning("IQ Option unavailable — emailing signals only this scan")

    try:
        amount = get_trade_amount(live_balance)

        # ── Phase A: Place ALL trades immediately ─────────────────────────
        # Each trade is placed back-to-back (~2-5s apart) so they all expire
        # within seconds of each other. No waiting happens here.
        placed: list[tuple[Signal, dict]] = []   # (signal, trade_dict)

        for sig in kept:
            name      = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
            iq_symbol = config.IQ_SYMBOLS.get(sig.symbol)
            can_trade = iq_client is not None and iq_symbol is not None

            if can_trade:
                logger.info(f"Placing trade: {name} {sig.direction} USD {amount:.2f}")
                trade = iq_client.place_trade(
                    symbol=iq_symbol,
                    direction=sig.direction,
                    amount=amount,
                    duration_minutes=config.EXPIRY_MINUTES,
                )
                if trade:
                    placed.append((sig, trade))
                    if not dry_run:
                        send_signal_email(sig, auto_trading=True, amount=amount)
                    continue   # skip the signal-only email below

            # Trade not placed → signal-only email
            if not dry_run:
                send_signal_email(sig, auto_trading=False, amount=amount)

        # ── Phase B: Wait ONCE for all trades to expire ───────────────────
        if placed:
            iq_client.wait_for_all([t for _, t in placed])

        # ── Phase C: Check results for all placed trades ──────────────────
        if placed:
            # Try check_win_v3 for each trade (trade-specific, no balance ambiguity)
            results: dict[int, dict] = {}   # trade_id → result
            known_net = 0.0

            for sig, trade in placed:
                result = iq_client.check_result(trade)
                if result:
                    results[trade["trade_id"]] = result
                    known_net += result["profit"]

            # Balance inference for any trade where check_win_v3 failed
            unknown = [(sig, trade) for sig, trade in placed if trade["trade_id"] not in results]

            if len(unknown) == 1:
                # Single unknown: total balance change minus known results = this trade
                sig, trade = unknown[0]
                final_balance = iq_client.refresh_balance()
                total_net     = round(final_balance - trade["balance_before"], 2)
                trade_net     = round(total_net - known_net, 2)
                outcome       = "WIN" if trade_net > 0 else "LOSS"
                logger.info(
                    f"RESULT (inferred): {trade['symbol']} {trade['direction']} → {outcome} | "
                    f"Net: USD {trade_net:+.2f} | Balance: USD {final_balance:.2f}"
                )
                results[trade["trade_id"]] = {
                    "outcome":     outcome,
                    "profit":      trade_net,
                    "stake":       amount,
                    "payout":      amount + trade_net if trade_net > 0 else 0.0,
                    "entry_spot":  0.0,
                    "exit_spot":   0.0,
                    "balance":     final_balance,
                    "currency":    "USD",
                    "contract_id": trade["trade_id"],
                }

            elif len(unknown) > 1:
                # Multiple unknowns: can't split balance change reliably
                # Fall back to individual balance method per trade
                for sig, trade in unknown:
                    result = iq_client._result_from_balance(trade)
                    if result:
                        results[trade["trade_id"]] = result
                        logger.warning(
                            f"Balance fallback for {trade['symbol']} may be inaccurate "
                            f"when multiple trades expire simultaneously — verify on portal"
                        )

            # Send result emails and log every confirmed result
            for sig, trade in placed:
                result = results.get(trade["trade_id"])
                if not result:
                    logger.warning(f"No result retrieved for trade {trade['trade_id']} — check IQ Option portal")
                    continue

                name    = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
                outcome = result["outcome"]
                profit  = result["profit"]
                balance = result["balance"]
                logger.info(
                    f"{'WIN' if outcome == 'WIN' else 'LOSS'}: {name} {sig.direction} | "
                    f"Profit: USD {profit:+.2f} | Balance: USD {balance:.2f}"
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
