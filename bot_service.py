"""
Long-running trading bot service — replaces the 5-minute GitHub Actions run.

Loop (single thread; every broker call has its own timeout):
  * SCAN_LEAD_SECONDS before each M5 close: fetch all pairs (one pipelined request), run the
    strategy + session / news / direction / correlation filters, record every
    6/10+ setup in the shadow log, and place all surviving trades at once.
    Turbo expiry snaps to the whole minute nearest now+5m, so each order spans
    exactly the next M5 candle.
  * Every LOOP_SECONDS: settle expired trades by order id (execution/deriv_broker.py or
    execution/iq_broker.py, chosen by config.BROKER).
    No 60s stagger, no balance diffing — any number of trades can be open.
  * Every minute: resolve matured shadow-log outcomes, publish status for the
    web app (heartbeat, IQ connection, balance, last scan).

State lives in SQLite (data/db.py), so a restart never loses an open trade: it
is reconciled by order id from the broker on the next loop. The
PWA (web_app.py) reads the same database. Notifications go out as web push
(notifications/notify.py); email is optional.

Usage:
    python bot_service.py             # run forever
    python bot_service.py --dry-run   # scan + shadow log only; no trades, no notifications
    python bot_service.py --once      # one scan now, settle, exit (for testing)
"""

import logging
import signal as signals
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.Formatter.converter = time.gmtime   # log in UTC, matching every time the bot reasons about
logger = logging.getLogger("bot")

import config
from data import db, shadow_log
from data.calendar import is_news_blocked, next_news_events
from data.fetcher import fetch_many
from data.indicators import add_all_indicators
from data.session import blackout_now, is_session_active, minutes_to_next_session, pair_session_now
from main import filter_correlated
from notifications import notify
from notifications.weekly_report import should_send_weekly
from strategy.trend_following import Signal, TrendFollowingStrategy

LOOP_SECONDS         = 2
MAX_SCAN_LATE_SECS   = 20    # beyond this an order would miss the candle-aligned expiry
HISTORY_AFTER_SECS   = 20    # query closed-options history once a trade is this late
HISTORY_EVERY_SECS   = 15
BREAKER_WINDOW_HOURS = 4
IQ_DOWN_ALERT_SECS   = 300   # push an alert when the broker has been unreachable this long
HEARTBEAT_FILE       = Path(db.DB_PATH).parent / "heartbeat"

_stop = False


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    once    = "--once" in sys.argv

    db.init()
    strategy = TrendFollowingStrategy()
    broker   = _make_broker(dry_run)

    signals.signal(signals.SIGTERM, _request_stop)
    signals.signal(signals.SIGINT, _request_stop)

    logger.info(
        f"Bot service started | {_mode(broker, dry_run)}"
        f" | OTC {'on' if config.ALLOW_OTC else 'off'} | min payout {config.MIN_PAYOUT:.0%} | db {db.DB_PATH}"
    )

    if once:
        scan(strategy, broker, dry_run)
        _settle_until_done(broker)
        shadow_log.resolve_pending()
        return

    next_scan       = _next_scan_time()
    last_minute_job = 0.0
    last_history    = 0.0
    last_weekly     = ""
    last_assets_log = 0.0
    status_start_set = False
    iq_down_since   = None
    iq_down_alerted = False
    db.set_status({"started_at": db.now_iso(), "mode": _mode(broker, dry_run)})

    while not _stop:
        if broker and not broker.ensure_connected():
            iq_down_since = iq_down_since or time.time()
            if not iq_down_alerted and broker.auth_rejected:
                notify.alert(f"{broker.name} rejected the login",
                             "Fix the broker credentials in .env, then: docker compose up -d. Retrying every 30 min.", tag="iq")
                iq_down_alerted = True
            elif not iq_down_alerted and time.time() - iq_down_since >= IQ_DOWN_ALERT_SECS:
                notify.alert(f"{broker.name} unreachable", "Reconnecting with backoff — no trades until it's back.", tag="iq")
                iq_down_alerted = True
            time.sleep(2)    # broker backs off internally; scans still run signal-only below
        elif broker and iq_down_since:
            if iq_down_alerted:
                notify.alert(f"{broker.name} reconnected", f"Down for {(time.time() - iq_down_since) / 60:.0f} min.", tag="iq")
            iq_down_since, iq_down_alerted = None, False

        now = time.time()    # after any reconnect, which can take up to 45s
        if now >= next_scan:
            late = now - next_scan
            if late > MAX_SCAN_LATE_SECS:
                # Placing now would snap to a misaligned expiry — wait for the next candle.
                logger.warning(f"Scan {late:.0f}s late — skipping this candle")
            else:
                try:
                    scan(strategy, broker, dry_run)
                except Exception:
                    logger.exception("Scan crashed — continuing")
            next_scan = _next_scan_time()
            logger.info(f"Next scan at {datetime.fromtimestamp(next_scan, timezone.utc):%H:%M:%S} UTC")
            db.set_status({"next_scan": datetime.fromtimestamp(next_scan, timezone.utc).isoformat()})

        if broker and broker.connected():
            try:
                last_history = settle_open_trades(broker, last_history)
            except Exception:
                logger.exception("Settlement pass crashed — will retry")
            if now - last_assets_log >= 1800:
                last_assets_log = now
                _log_assets(broker)

        if now - last_minute_job >= 60:
            last_minute_job = now
            shadow_log.resolve_pending()
            HEARTBEAT_FILE.touch()
            db.set_status({
                "heartbeat":    db.now_iso(),
                "iq_connected": int(bool(broker and broker.connected())),
                "balance":      f"{broker.balance:.2f}" if broker else None,
            })
            if broker and broker.balance and not status_start_set:
                # First balance seen on this broker's account = start of its equity curve.
                key = f"start_balance_{config.BROKER}"
                if not db.get_status().get(key):
                    db.set_status({key: f"{broker.balance:.2f}"})
                status_start_set = True
            today = datetime.now(timezone.utc).date().isoformat()
            if not dry_run and should_send_weekly() and last_weekly != today:
                last_weekly = today
                try:
                    notify.weekly(db.journal_rows(config.BROKER))
                except Exception:
                    logger.exception("Weekly report failed")

        time.sleep(LOOP_SECONDS)

    logger.info("Stopping — open trades stay OPEN in the database and settle on next start")
    if broker:
        broker.close()


# ── Scan + placement ──────────────────────────────────────────────────────

_breaker_alerted = ""


def scan(strategy: TrendFollowingStrategy, broker, dry_run: bool) -> None:
    global _breaker_alerted
    breaker = circuit_breaker()
    if breaker:
        logger.warning(f"CIRCUIT BREAKER ACTIVE: {breaker} — scanning for shadow log only")
        if breaker != _breaker_alerted and not dry_run:
            notify.alert("Circuit breaker tripped", f"{breaker}. Trading paused; resumes automatically.", tag="breaker")
    _breaker_alerted = breaker

    t0 = time.time()
    frames = _fetch_all(config.TRADING_PAIRS)
    logger.info(f"Fetched {len(frames)} pairs in {time.time() - t0:.1f}s")

    candidates: list[tuple[Signal, int | None, str]] = []
    for symbol, (df_m5, df_h1) in frames.items():
        name = config.PAIR_DISPLAY.get(symbol, symbol)
        if df_m5 is None:
            logger.warning(f"SKIP {name}: insufficient data")
            continue
        last_candle = df_m5.index[-1].to_pydatetime()
        if datetime.now(timezone.utc) - last_candle > timedelta(minutes=shadow_log.STALE_MINUTES):
            # Weekend / holiday: sessions don't know the market is shut, and the last
            # Friday setup would otherwise re-signal every cooldown period.
            logger.info(f"SKIP {name}: market closed (last candle {last_candle:%a %H:%M} UTC)")
            continue

        session_ok, session_info = is_session_active(symbol)
        blocked, news_reason = is_news_blocked(symbol)
        shadow_id = shadow_log.record(
            symbol, df_m5, strategy.features(df_m5, htf_df=df_h1),
            session=pair_session_now(symbol), blackout=blackout_now(), news_blocked=blocked,
        )

        if not session_ok:
            logger.info(f"SKIP {name}: {session_info} — {minutes_to_next_session(symbol)} min to next session")
            continue
        if blocked:
            logger.info(f"SKIP {name}: {news_reason}")
            continue

        sig = strategy.analyze(symbol, df_m5, htf_df=df_h1)
        if not sig.is_actionable:
            logger.info(
                f"NO SIGNAL {name} | ADX={sig.adx:.1f} RSI={sig.rsi:.1f} HTF={sig.htf_trend} "
                f"passed={len(sig.checks_passed)}/10 | failed=[{', '.join(sig.checks_failed[:4]) or 'none logged'}]"
            )
            continue
        allowed_dir = config.PAIR_DIRECTION_RESTRICT.get(symbol)
        if allowed_dir and sig.direction != allowed_dir:
            logger.info(f"DIRECTION BLOCK {name} {sig.direction}: only {allowed_dir} allowed")
            continue

        sig.upcoming_news = next_news_events(symbol, look_ahead_hours=2)
        candidates.append((sig, shadow_id, session_info))
        logger.info(
            f"SIGNAL {name} {sig.direction} {sig.confidence_label} {sig.strength:.0%} | "
            f"ADX={sig.adx:.1f} RSI={sig.rsi:.1f} MACD={sig.macd_hist:+.6f} BB={sig.bb_width*100:.3f}%"
        )

    kept_syms = {s.symbol for s in filter_correlated([c[0] for c in candidates])}
    kept = [c for c in candidates if c[0].symbol in kept_syms]
    for _, shadow_id, _ in kept:
        shadow_log.mark(shadow_id, "live")

    db.set_status({"last_scan": db.now_iso(), "last_scan_signals": len(kept)})
    if breaker or not kept:
        logger.info(f"Scan complete — {len(kept)} signal(s){', breaker active' if breaker else ''}")
        return

    can_trade = broker is not None and broker.connected() and not dry_run
    busy = db.pairs_traded_since(config.SIGNAL_COOLDOWN_MINUTES) | {t["pair"] for t in db.open_trades()}
    if can_trade:
        broker.refresh_balance()

    for sig, shadow_id, session_info in kept:
        name = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
        base = broker.symbol_for(sig.symbol) if broker else None
        placed, reason = None, ""
        amount = _stake(broker.balance if can_trade else None)

        if not can_trade:
            reason = "dry run" if dry_run else (f"{broker.name} not connected" if broker else "signal-only mode")
        elif not base:
            reason = f"{name} is not tradeable on {broker.name}"
        elif sig.symbol in busy:
            reason = f"traded < {config.SIGNAL_COOLDOWN_MINUTES} min ago or still open"
            logger.info(f"COOLDOWN {name}: {reason} — notification only")
        else:
            placed, reason = _place(broker, sig, base, amount, session_info)

        if placed:
            shadow_log.mark(shadow_id, "traded")
        if not dry_run:
            notify.signal(sig, placed=bool(placed), amount=placed or amount, reason=reason)

    logger.info(f"Scan complete — {len(kept)} signal(s) processed")


def _place(broker, sig: Signal, base: str, amount: float, session_info: str) -> tuple[float | None, str]:
    """Place one order and record it OPEN. Returns (stake, "") or (None, why it wasn't placed)."""
    name = config.PAIR_DISPLAY.get(sig.symbol, sig.symbol)
    ticker, why = broker.ticker_for(base)
    if not ticker:
        logger.info(f"NOT PLACED {name}: {why}")
        return None, why
    res, why = broker.buy(ticker, sig.direction, amount, config.BROKER_EXPIRY_MINUTES, config.MIN_PAYOUT)
    if not res:
        logger.info(f"NOT PLACED {name}: {why}")
        return None, why
    order_id, expiry, payout, amount = res["order_id"], res["expiry"], res["payout"], res["stake"]
    db.insert_open_trade({
        "order_id":      order_id,
        "broker":        config.BROKER,
        "account":       getattr(broker, "account_id", None) or ("demo" if config.IQ_DEMO else "real"),
        "placed_at":     db.now_iso(),
        "expires_at":    datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
        "pair":          sig.symbol,
        "ticker":        ticker,
        "direction":     sig.direction,
        "session":       session_info,
        "confidence":    sig.confidence_label,
        "strength":      sig.strength,
        "num_passed":    len(sig.checks_passed),
        "checks_passed": "|".join(sig.checks_passed),
        "checks_failed": "|".join(sig.checks_failed),
        "price":         float(sig.price),
        "adx":           float(sig.adx),
        "rsi":           float(sig.rsi),
        "atr":           float(sig.atr),
        "macd_hist":     float(sig.macd_hist),
        "bb_width":      float(sig.bb_width),
        "htf_trend":     sig.htf_trend,
        "pattern":       sig.candlestick_pattern,
        "stake":         amount,
        "payout":        payout,
    })
    logger.info(
        f"TRADE PLACED {ticker} {sig.direction} USD {amount:.2f} | id={order_id} | "
        f"expires {datetime.fromtimestamp(expiry, timezone.utc):%H:%M:%S} UTC"
        + (f" | payout {payout:.0%}" if payout is not None else "")
    )
    return amount, ""


# ── Settlement ────────────────────────────────────────────────────────────

def settle_open_trades(broker, last_history: float) -> float:
    """Settle every expired OPEN trade by order id. Returns the updated history timestamp."""
    now = datetime.now(timezone.utc)
    history: dict | None = None
    for t in db.open_trades():
        late = (now - datetime.fromisoformat(t["expires_at"])).total_seconds()
        if late < 2:
            continue

        res = broker.pushed_result(t["order_id"], t["stake"], t["ticker"])
        if res and res[1] is None:
            res = None   # outcome known but not the amount — let history supply it
        if res is None and late >= HISTORY_AFTER_SECS and time.time() - last_history >= HISTORY_EVERY_SECS:
            if history is None:
                history = broker.history_results(50)
                last_history = time.time()
            res = history.get(t["order_id"])
        if res is None and late >= config.RESULT_GIVE_UP_SECONDS:
            pushed = broker.pushed_result(t["order_id"], t["stake"], t["ticker"])
            res = pushed if pushed else ("UNKNOWN", None, "none")
            logger.warning(f"No full result for {t['order_id']} after {late:.0f}s — recording {res[0]}")
        if res is None:
            continue

        status, profit, source = res
        balance = broker.refresh_balance()
        db.settle_trade(t["order_id"], status, profit, balance, source)
        logger.info(
            f"{status}: {config.PAIR_DISPLAY.get(t['pair'], t['pair'])} {t['direction']} | "
            f"Profit: USD {profit if profit is not None else 0:+.2f} | Balance: USD {balance:.2f} | via {source}"
        )
        _after_settle(t, status, profit, balance)
    return last_history


def _after_settle(t: dict, status: str, profit: float | None, balance: float) -> None:
    if "--dry-run" in sys.argv:
        return
    sig = _signal_from_row(t)
    result = {
        "outcome":     status,
        "profit":      profit if profit is not None else 0.0,
        "stake":       t["stake"],
        "payout":      t["stake"] + profit if (status == "WIN" and profit is not None) else 0.0,
        "entry_spot":  t["price"],
        "exit_spot":   0.0,
        "balance":     balance,
        "currency":    t["currency"] or "USD",
        "contract_id": t["order_id"],
    }
    try:
        notify.result(sig, result, t["stake"])
    except Exception as e:
        logger.error(f"Result notification failed: {e}")


def _settle_until_done(broker, max_wait: int = 900) -> None:
    deadline, last_history = time.time() + max_wait, 0.0
    while broker and db.open_trades() and time.time() < deadline:
        if broker.ensure_connected():
            last_history = settle_open_trades(broker, last_history)
        time.sleep(LOOP_SECONDS)


# ── Helpers ───────────────────────────────────────────────────────────────

def circuit_breaker() -> str:
    """Reason string if the last N settled trades are all losses within the window, else ''."""
    n = config.MAX_CONSECUTIVE_LOSSES
    tail = db.recent_settled(n, broker=config.BROKER)
    if len(tail) < n or any(t["status"] != "LOSS" for t in tail):
        return ""
    oldest = min(datetime.fromisoformat(t["settled_at"]) for t in tail)
    if datetime.now(timezone.utc) - oldest > timedelta(hours=BREAKER_WINDOW_HOURS):
        return ""
    return f"{n} consecutive losses within {BREAKER_WINDOW_HOURS}h"


def _fetch_all(symbols: list[str]) -> dict[str, tuple]:
    """{symbol: (df_m5 with indicators | None, df_h1 | None)} — one pipelined Deriv round trip."""
    m5 = lambda s: (s, config.SIGNAL_INTERVAL, config.SIGNAL_PERIOD)
    h1 = lambda s: (s, config.TREND_INTERVAL, config.TREND_PERIOD)
    got = fetch_many([k for s in symbols for k in (m5(s), h1(s))])
    out = {}
    for s in symbols:
        df_m5 = got.get(m5(s))
        try:
            out[s] = (add_all_indicators(df_m5), got.get(h1(s))) if df_m5 is not None and len(df_m5) >= 60 else (None, None)
        except Exception as e:
            logger.error(f"Indicators failed for {s}: {e}")
            out[s] = (None, None)
    return out


def _next_scan_time() -> float:
    period = 300
    now = time.time()
    t = (now // period + 1) * period - config.SCAN_LEAD_SECONDS
    return t if t > now + 1 else t + period


def _stake(balance: float | None) -> float:
    bal = balance if balance and balance > 0 else config.ACCOUNT_BALANCE
    return round(max(config.MIN_TRADE_AMOUNT, min(config.MAX_TRADE_AMOUNT, bal * config.TRADE_AMOUNT_PCT)), 2)


def _signal_from_row(t: dict) -> Signal:
    return Signal(
        symbol=t["pair"], direction=t["direction"], strength=t["strength"] or 0.0,
        confidence_label=t["confidence"] or "", checks_passed=(t["checks_passed"] or "").split("|"),
        checks_failed=(t["checks_failed"] or "").split("|"), price=t["price"] or 0.0,
        adx=t["adx"] or 0.0, rsi=t["rsi"] or 0.0, ema_fast=0.0, ema_slow=0.0, atr=t["atr"] or 0.0,
        htf_trend=t["htf_trend"] or "", candlestick_pattern=t["pattern"] or "",
        macd_hist=t["macd_hist"] or 0.0, bb_width=t["bb_width"] or 0.0, volume_ok=False, advice="",
    )


def _log_assets(broker) -> None:
    """Log what the broker offers right now for every scanned pair, auto-traded or not."""
    try:
        avail = broker.availability([s.replace("=X", "") for s in config.TRADING_PAIRS])
    except Exception as e:
        logger.warning(f"{broker.name} asset check failed: {e}")
        return
    for sym in config.TRADING_PAIRS:
        base = sym.replace("=X", "")
        auto = "auto-trade" if broker.symbol_for(sym) else "signal-only"
        logger.info(f"{broker.name} asset {base:<7} [{auto}]: {avail.get(base, '?')}")
    db.set_status({"broker_assets": "; ".join(f"{k}: {v}" for k, v in avail.items())})


def _mode(broker, dry_run: bool) -> str:
    if dry_run:
        return "DRY RUN"
    if not broker:
        return "SIGNAL-ONLY"
    demo = getattr(broker, "is_demo", True)
    return f"AUTO-TRADE · {broker.name} {'DEMO' if demo else 'LIVE'}"


def _make_broker(dry_run: bool):
    if dry_run:
        return None
    if config.BROKER == "deriv":
        if not (config.DERIV_API_TOKEN and config.DERIV_TRADE_APP_ID and config.DERIV_ACCOUNT_ID):
            logger.warning("BROKER=deriv but DERIV_API_TOKEN / DERIV_TRADE_APP_ID / DERIV_ACCOUNT_ID missing")
            return None
        from execution.deriv_broker import DerivBroker
        broker = DerivBroker(config.DERIV_API_TOKEN, config.DERIV_TRADE_APP_ID, config.DERIV_ACCOUNT_ID)
    else:
        if not (config.IQ_EMAIL and config.IQ_PASSWORD):
            return None
        from execution.iq_broker import IQBroker
        broker = IQBroker(config.IQ_EMAIL, config.IQ_PASSWORD, demo=config.IQ_DEMO, allow_otc=config.ALLOW_OTC)
    broker.ensure_connected()
    db.set_status({"broker": broker.name, "broker_key": config.BROKER})
    return broker


def _request_stop(*_):
    global _stop
    _stop = True


if __name__ == "__main__":
    main()
