"""
Single-scan entry point — triggered by cron-job.org every 5 minutes.

Flow:
  1. Session + news + strategy + correlation filters → signals
  2. Connect to IQ Option ONCE
  3. Phase A — Place trades with TRADE_PLACEMENT_DELAY between each
               (staggered expiries so balance method works per-trade)
  4. Phase B — Wait for each trade's individual expiry, check result,
               then move to the next — balance reflects one trade at a time
  5. Disconnect

Result detection strategy:
  - Try check_win_v3 once (fast if WebSocket is fresh)
  - Fall back to balance comparison — reliable because trades expire
    60 seconds apart, so only ONE result hits the balance at a time

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
from data.trade_journal import log_trade, circuit_breaker_check, get_recently_traded_pairs
from strategy.trend_following import TrendFollowingStrategy, Signal
from notifications.alerts import send_signal_email, send_trade_result_email
from notifications.weekly_report import should_send_weekly, send_weekly_report
from main import filter_correlated

# Seconds between trade placements.
# Creates a 60-second gap between expiries so balance method works per-trade.
TRADE_PLACEMENT_DELAY = 60


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

    # ── Circuit breakers (checked before any market work) ─────────────────
    if not dry_run:
        can_trade, cb_reason = circuit_breaker_check()
        if not can_trade:
            logger.warning(f"CIRCUIT BREAKER ACTIVE: {cb_reason}")
            return

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
        # ── Phase A: Place trades with 60-second gap between each ─────────
        # The gap makes expiries stagger so balance method works per-trade.
        placed: list[tuple[Signal, dict, float]] = []   # (signal, trade, amount)

        # Cross-run per-pair cooldown: fetch once before the loop.
        # GitHub Actions runs are stateless — without this the same pair can
        # fire an auto-trade every 5 minutes across independent runs.
        recently_traded: set[str] = set()
        if iq_client is not None and not dry_run:
            recently_traded = get_recently_traded_pairs(cooldown_minutes=config.SIGNAL_COOLDOWN_MINUTES)
            if recently_traded:
                names = ", ".join(config.PAIR_DISPLAY.get(p, p) for p in recently_traded)
                logger.info(f"Cross-run cooldown active for: {names} (trade placed < {config.SIGNAL_COOLDOWN_MINUTES} min ago)")

        trades_placed = 0   # counts successful placements; used to stagger delays

        for sig in kept:
            name      = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
            iq_symbol = config.IQ_SYMBOLS.get(sig.symbol)
            can_trade = iq_client is not None and iq_symbol is not None

            # Skip auto-trade if this pair was already traded in the last cooldown window
            if can_trade and sig.symbol in recently_traded:
                logger.warning(
                    f"COOLDOWN {name}: auto-trade placed < {config.SIGNAL_COOLDOWN_MINUTES} min ago "
                    f"— sending signal email only"
                )
                can_trade = False

            # Stagger placements — skip delay before first actual placement
            if can_trade and trades_placed > 0:
                logger.info(f"Waiting {TRADE_PLACEMENT_DELAY}s before next placement (staggering expiries) ...")
                time.sleep(TRADE_PLACEMENT_DELAY)

            # Refresh balance before each placement so amount is accurate
            if can_trade:
                current_balance = iq_client.refresh_balance()
                # Sanity: abort remaining trades if balance dropped > 10% since scan started
                if live_balance and current_balance < live_balance * 0.90:
                    logger.warning(
                        f"Balance dropped from USD {live_balance:.2f} to "
                        f"USD {current_balance:.2f} (>{10:.0f}% drawdown this scan) — "
                        f"halting remaining placements"
                    )
                    break

            amount = get_trade_amount(iq_client.balance if can_trade else live_balance)

            if can_trade:
                logger.info(f"Placing trade: {name} {sig.direction} USD {amount:.2f}")
                trade = iq_client.place_trade(
                    symbol=iq_symbol,
                    direction=sig.direction,
                    amount=amount,
                    duration_minutes=config.EXPIRY_MINUTES,
                )
                # Retry once after 90s for brief H1-boundary pauses.
                # Skip retry only if a news event is active — the IQ Option
                # suspension is protective in that case and the market will
                # have already moved on the news release.
                if trade is None:
                    news_now, news_reason = is_news_blocked(sig.symbol)
                    if news_now:
                        logger.info(
                            f"Skipping retry for {name} — news event active ({news_reason})"
                        )
                    else:
                        logger.info(f"Placement rejected — waiting 90s then retrying {name} ...")
                        time.sleep(90)
                        iq_client.refresh_balance()
                        amount = get_trade_amount(iq_client.balance)
                        trade = iq_client.place_trade(
                            symbol=iq_symbol,
                            direction=sig.direction,
                            amount=amount,
                            duration_minutes=config.EXPIRY_MINUTES,
                        )
                if trade:
                    placed.append((sig, trade, amount))
                    trades_placed += 1
                    if not dry_run:
                        send_signal_email(sig, auto_trading=True, amount=amount)
                    continue

            # Trade not placed → signal-only email
            if not dry_run:
                send_signal_email(sig, auto_trading=False, amount=amount)

        # ── Phase B: Check each trade's result individually ───────────────
        # Trades expire 60s apart. We wait for each one, check its balance
        # impact alone, then move to the next. No multi-trade ambiguity.
        if placed:
            # Refresh once after all stakes are placed — this is the true
            # pre-expiry balance (stakes already deducted by IQ Option).
            pre_balance = iq_client.refresh_balance()
            logger.info(f"Pre-expiry balance (all stakes out): USD {pre_balance:.2f}")

            for sig, trade, amount in placed:
                name = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)

                # Wait for this specific trade's expiry
                iq_client._wait_until(trade["expiry_epoch"], label=f"{trade['symbol']} {trade['trade_id']}")

                # Try check_win_v3 once (fast path, rarely works but costs only 15s)
                result = iq_client.check_result(trade, max_retries=1)

                if result is None:
                    # Balance method — reliable because this is the only
                    # trade that has just expired (others expire 60s later)
                    new_balance  = iq_client.refresh_balance()
                    net          = round(new_balance - pre_balance, 2)
                    outcome      = "WIN" if net > amount * 0.5 else "LOSS"
                    profit       = round(net - amount, 2) if outcome == "WIN" else -amount
                    logger.info(
                        f"RESULT (balance): {trade['symbol']} {trade['direction']} → {outcome} | "
                        f"Net: USD {net:+.2f} | Profit: USD {profit:+.2f} | Balance: USD {new_balance:.2f}"
                    )
                    result = {
                        "outcome":     outcome,
                        "profit":      profit,
                        "stake":       amount,
                        "payout":      new_balance - pre_balance if outcome == "WIN" else 0.0,
                        "entry_spot":  0.0,
                        "exit_spot":   0.0,
                        "balance":     new_balance,
                        "currency":    "USD",
                        "contract_id": trade["trade_id"],
                    }
                    pre_balance = new_balance   # update for next trade

                else:
                    # check_win_v3 worked — still update pre_balance
                    pre_balance = result["balance"]

                logger.info(
                    f"{'WIN' if result['outcome'] == 'WIN' else 'LOSS'}: {name} {sig.direction} | "
                    f"Profit: USD {result['profit']:+.2f} | Balance: USD {result['balance']:.2f}"
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
