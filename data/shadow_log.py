"""
Shadow (paper) log — every reasonable setup the strategy sees, traded or not,
with what price actually did 5 / 15 / 30 minutes later.

Why: real trades arrive at ~2-8 per day, far too slowly to tell a good filter
from a lucky one (the Jul 19-20 gates were fitted on 6-34 trades and cost 24%
of the balance). Shadow rows arrive at dozens per day, risk nothing, and carry
the raw indicator values plus a flag for every gate, so shadow_report.py can
replay any filter combination — and any expiry — over the same data.

Rows live in the `shadow` table of data/db.py.
"""

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
from data import db, deriv_feed

logger = logging.getLogger(__name__)

MIN_PASSED     = 6              # log setups scoring >= 6/10 so looser thresholds can be tested
HORIZONS       = (5, 15, 30)    # minutes after entry to record the outcome
STALE_MINUTES  = 15             # newest M5 candle older than this = market closed, skip
MAX_RESOLVE_H  = 20             # give up on rows the 1-day M1 history can no longer cover


def record(symbol: str, df_m5: pd.DataFrame, feats: dict | None,
           session: str, blackout: str, news_blocked: bool) -> int | None:
    """Store this scan's setup for `symbol` if worth logging. Returns the row id."""
    if feats is None or feats["num_passed"] < MIN_PASSED:
        return None
    now = datetime.now(timezone.utc)
    if now - df_m5.index[-1].to_pydatetime() > timedelta(minutes=STALE_MINUTES):
        return None   # weekend / holiday — stale prices would resolve as ties
    # Mirror the live per-pair cooldown so consecutive scans of one setup count once.
    since = (now - timedelta(minutes=config.SIGNAL_COOLDOWN_MINUTES)).isoformat()
    if db.shadow_logged_since(symbol, feats["direction"], since):
        return None
    return db.insert_shadow({
        "ts":           now.replace(microsecond=0).isoformat(),
        "pair":         symbol,
        "direction":    feats["direction"],
        "num_passed":   int(feats["num_passed"]),
        "checks":       feats["checks"],
        "price":        float(feats["price"]),
        "adx":          float(feats["adx"]),
        "plus_di":      float(feats["plus_di"]),
        "minus_di":     float(feats["minus_di"]),
        "rsi":          float(feats["rsi"]),
        "atr":          float(feats["atr"]),
        "macd_hist":    float(feats["macd_hist"]),
        "bb_width":     float(feats["bb_width"]),
        "htf_trend":    str(feats["htf_trend"]),
        "pattern":      str(feats["pattern"] or ""),
        "session":      session,
        "blackout":     blackout,
        "news_blocked": int(news_blocked),
    })


def mark(row_id: int | None, field: str) -> None:
    """Flag a shadow row as `live` (signal emitted) or `traded` (order placed)."""
    if row_id is not None:
        db.shadow_update(row_id, {field: 1})


def resolve_pending() -> int:
    """Fill px_* / res_* for horizons that have matured. Never raises. Returns rows changed."""
    try:
        return _resolve_pending()
    except Exception as e:
        logger.error(f"Shadow resolve failed: {e}")
        return 0


def _resolve_pending() -> int:
    now = datetime.now(timezone.utc)
    m1_cache: dict[str, pd.DataFrame | None] = {}
    changed = 0
    for r in db.shadow_pending():
        entry_t = datetime.fromisoformat(r["ts"])
        pending = [h for h in HORIZONS if r[f"res_{h}"] is None]
        if now - entry_t > timedelta(hours=MAX_RESOLVE_H):
            db.shadow_update(r["id"], {f"res_{h}": "EXPIRED" for h in pending})
            changed += 1
            continue
        due = [h for h in pending if now >= entry_t + timedelta(minutes=h, seconds=90)]
        if not due:
            continue
        if r["pair"] not in m1_cache:
            m1_cache[r["pair"]] = deriv_feed.fetch_ohlcv(r["pair"], interval="1m", period="1d")
        m1 = m1_cache[r["pair"]]
        if m1 is None or m1.empty:
            continue
        # Entry and exit both read from M1 closes so they share the same <=60s lag.
        entry = _price_at(m1, entry_t)
        if entry is None:
            continue
        update: dict = {"px_entry": entry}
        for h in due:
            exit_ = _price_at(m1, entry_t + timedelta(minutes=h))
            if exit_ is None:
                continue
            update[f"px_{h}"] = exit_
            if exit_ == entry:
                update[f"res_{h}"] = "TIE"
            else:
                update[f"res_{h}"] = "WIN" if (exit_ > entry) == (r["direction"] == "BUY") else "LOSS"
        db.shadow_update(r["id"], update)
        changed += 1
    if changed:
        logger.info(f"Shadow log: {changed} row(s) resolved")
    return changed


def _price_at(m1: pd.DataFrame, t: datetime) -> float | None:
    """Close of the last M1 candle that had finished by time t."""
    closed = m1[m1.index + pd.Timedelta(minutes=1) <= t]
    if closed.empty:
        return None
    col = "Close" if "Close" in closed.columns else "close"
    return float(closed[col].iloc[-1])
