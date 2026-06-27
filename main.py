"""
Pocket Option Signal Bot
Scans Forex pairs on M5 + H1, fires only high-confidence signals, emails them to you.

Filters applied in order every scan cycle:
  1. Session filter     — high-liquidity window only
  2. Calendar filter    — block ±30 min around high-impact news
  3. Strategy engine    — 10-confluence trend check (min 75% = 8/10)
  4. Correlation filter — drop weaker duplicate when two correlated pairs signal together

Run:   python main.py
Stop:  Ctrl+C
"""

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

import config
from data.fetcher import fetch_ohlcv
from data.indicators import add_all_indicators
from data.session import is_session_active, active_sessions_now, minutes_to_next_session
from data.calendar import is_news_blocked, next_news_events
from strategy.trend_following import TrendFollowingStrategy, Signal
from risk.manager import RiskManager
from notifications.alerts import send_signal_email, send_startup_email, send_daily_summary

colorama_init(autoreset=True)

# ── Logging ───────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

SCAN_INTERVAL = 300   # 5 minutes — one full M5 candle


# ── Correlation filter ────────────────────────────────────────────────────

def filter_correlated(signals: list[Signal]) -> list[Signal]:
    """
    For each correlation group, if two or more signals fired the same direction,
    keep only the strongest one and drop the rest.
    Signals with different directions in the same group are kept (contradictions
    are rare but valid — different timeframe dynamics).
    """
    dropped: set[str] = set()

    for group in config.CORRELATION_GROUPS:
        group_sigs = [s for s in signals if s.symbol in group and s.symbol not in dropped]
        # Separate by direction
        for direction in ("BUY", "SELL"):
            dir_sigs = [s for s in group_sigs if s.direction == direction]
            if len(dir_sigs) < 2:
                continue
            # Keep the strongest, drop the rest
            dir_sigs.sort(key=lambda s: s.strength, reverse=True)
            for weak in dir_sigs[1:]:
                dropped.add(weak.symbol)
                logger.info(
                    f"CORRELATION DROP: {weak.symbol} {direction} ({weak.strength:.0%}) "
                    f"— superseded by {dir_sigs[0].symbol} ({dir_sigs[0].strength:.0%})"
                )

    kept = [s for s in signals if s.symbol not in dropped]
    return kept


# ── Terminal display ──────────────────────────────────────────────────────

def print_banner():
    from data import oanda
    data_src = "OANDA (real-time)" if oanda.is_configured() else "yfinance (may lag ~15 min)"
    sessions = ", ".join(active_sessions_now()) or "None (off-peak)"
    print(Fore.CYAN + Style.BRIGHT + r"""
  ╔══════════════════════════════════════════════════════════╗
  ║   POCKET OPTION SIGNAL BOT  —  Tier-1 Accuracy Mode    ║
  ║   10 checks: EMA+ADX+RSI+MACD+BB+HTF+Pattern+Session   ║
  ╚══════════════════════════════════════════════════════════╝""")
    print(f"  Pairs   : {', '.join(config.PAIR_DISPLAY.get(p, p) for p in config.TRADING_PAIRS)}")
    print(f"  Data    : {data_src}")
    print(f"  Sessions: {sessions}")
    print(f"  Email   : {config.EMAIL_RECIPIENT or '(not configured)'}\n")


def print_signal(sig: Signal, session_name: str, dropped_pairs: list[str] = None):
    color = Fore.GREEN if sig.direction == "BUY" else Fore.RED
    name  = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
    arrow = "↑ CALL" if sig.direction == "BUY" else "↓ PUT"
    ts    = sig.timestamp.strftime("%H:%M:%S UTC")
    macd_str = f"{sig.macd_hist:+.6f}"
    bb_str   = f"{sig.bb_width*100:.3f}%"
    print(color + Style.BRIGHT + f"\n  [{ts}]  {arrow}  {name}  |  {sig.confidence_label}  {sig.strength:.0%}")
    print(color + f"  Price {sig.price:.5f}  ADX {sig.adx:.1f}  RSI {sig.rsi:.1f}  H1: {sig.htf_trend}")
    print(color + f"  MACD hist: {macd_str}  BB width: {bb_str}  Session: {session_name}")
    print(color + f"  Pattern: {sig.candlestick_pattern or 'none'}  |  {len(sig.checks_passed)}/10 checks passed")
    if dropped_pairs:
        print(Fore.YELLOW + f"  (Suppressed correlated: {', '.join(dropped_pairs)})")
    print()


def print_skip(symbol: str, reason: str, color=Fore.YELLOW):
    name = config.PAIR_DISPLAY.get(symbol, symbol)
    print(color + f"  SKIP  {name:<10}  {reason}          ", end="\r")


def print_scan_tick():
    ts       = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    sessions = ", ".join(active_sessions_now()) or "off-peak"
    print(Fore.WHITE + f"  [{ts}]  Scanning ... sessions: {sessions}          ", end="\r")


# ── Data helpers ──────────────────────────────────────────────────────────

def fetch_with_indicators(symbol: str, interval: str, period: str):
    df = fetch_ohlcv(symbol, interval=interval, period=period)
    if df is None or len(df) < 60:
        return None
    return add_all_indicators(df)


# ── Core scan ─────────────────────────────────────────────────────────────

def scan_once(strategy: TrendFollowingStrategy, signals_today: list[Signal]):
    print_scan_tick()
    candidate_signals: list[tuple[Signal, str]] = []   # (signal, session_name)

    for symbol in config.TRADING_PAIRS:

        # FILTER 1 — Session
        session_ok, session_info = is_session_active(symbol)
        if not session_ok:
            wait = minutes_to_next_session(symbol)
            print_skip(symbol, f"off-peak — {wait} min to next session")
            continue

        # FILTER 2 — News calendar
        news_blocked, news_reason = is_news_blocked(symbol)
        if news_blocked:
            print_skip(symbol, news_reason, color=Fore.MAGENTA)
            logger.info(f"SKIP {symbol} (news): {news_reason}")
            continue

        # FILTER 3 — Fetch + strategy
        df_m5 = fetch_with_indicators(symbol, config.SIGNAL_INTERVAL, config.SIGNAL_PERIOD)
        if df_m5 is None:
            logger.debug(f"SKIP {symbol}: no M5 data")
            continue

        df_h1 = fetch_ohlcv(symbol, interval=config.TREND_INTERVAL, period=config.TREND_PERIOD)
        sig   = strategy.analyze(symbol, df_m5, htf_df=df_h1)

        if sig.is_actionable:
            sig.upcoming_news = next_news_events(symbol, look_ahead_hours=2)
            candidate_signals.append((sig, session_info))
        else:
            logger.debug(f"NO SIGNAL {symbol}: {sig.advice}")

    if not candidate_signals:
        return

    # FILTER 4 — Correlation: drop weaker duplicates
    raw_sigs    = [s for s, _ in candidate_signals]
    session_map = {s.symbol: sess for s, sess in candidate_signals}
    kept_sigs   = filter_correlated(raw_sigs)
    kept_syms   = {s.symbol for s in kept_sigs}
    dropped     = [s.symbol for s in raw_sigs if s.symbol not in kept_syms]

    for sig in kept_sigs:
        if len(signals_today) >= config.MAX_DAILY_SIGNALS:
            logger.info("Daily signal cap reached.")
            return

        session_name = session_map.get(sig.symbol, "")
        corr_dropped = [d for d in dropped]   # pairs dropped because of this signal
        print_signal(sig, session_name, corr_dropped if corr_dropped else None)
        signals_today.append(sig)

        sent = send_signal_email(sig)
        status = "sent" if sent else "FAILED (check .env)"
        logger.info(f"Email {status}: {sig.symbol} {sig.direction} {sig.confidence_label} {sig.strength:.0%}")


# ── Entry point ───────────────────────────────────────────────────────────

def main():
    print_banner()
    strategy      = TrendFollowingStrategy()
    risk          = RiskManager(balance=config.ACCOUNT_BALANCE)
    signals_today: list[Signal] = []
    last_day      = datetime.now(timezone.utc).date()

    send_startup_email()
    logger.info(f"Bot running — scanning every {SCAN_INTERVAL}s. Ctrl+C to stop.")

    try:
        while True:
            today = datetime.now(timezone.utc).date()
            if today != last_day:
                if signals_today:
                    send_daily_summary(risk.stats())
                signals_today = []
                last_day = today

            scan_once(strategy, signals_today)
            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n\n  Bot stopped.")
        if signals_today:
            print(f"  Signals sent today: {len(signals_today)}")
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
