"""
Economic calendar filter.

Fetches the current week's high-impact news events from a free ForexFactory
feed and blocks trading on any pair whose currencies have an event within
the configured buffer window.

Data source: https://nfs.faireconomy.media/ff_calendar_thisweek.json
Cached in memory for 60 minutes so we don't spam the endpoint.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

# Currencies to watch for each pair (symbol → [base, quote])
PAIR_CURRENCIES = {
    "EURUSD=X": ["EUR", "USD"],
    "GBPUSD=X": ["GBP", "USD"],
    "USDJPY=X": ["USD", "JPY"],
    "AUDUSD=X": ["AUD", "USD"],
    "USDCAD=X": ["USD", "CAD"],
    "EURGBP=X": ["EUR", "GBP"],
    "GBPJPY=X": ["GBP", "JPY"],
}

CALENDAR_URL   = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BLOCK_BEFORE   = 30   # minutes before event — don't trade
BLOCK_AFTER    = 30   # minutes after event — don't trade
CACHE_MINUTES  = 60   # refresh calendar every 60 minutes

_cache: list[dict] = []
_cache_ts: float   = 0.0


# ── Public API ────────────────────────────────────────────────────────────

def is_news_blocked(symbol: str) -> tuple[bool, str]:
    """
    Returns (True, reason) if a high-impact news event is within the block
    window for either currency in the pair. Returns (False, "") if clear.
    """
    events  = _get_events()
    now_utc = datetime.now(timezone.utc)
    currencies = PAIR_CURRENCIES.get(symbol, [])

    for ev in events:
        if ev.get("impact", "").lower() != "high":
            continue
        if ev.get("country", "").upper() not in currencies:
            continue

        ev_time = _parse_event_time(ev.get("date", ""))
        if ev_time is None:
            continue

        delta = (ev_time - now_utc).total_seconds() / 60   # minutes

        if -BLOCK_AFTER <= delta <= BLOCK_BEFORE:
            direction = "in" if delta >= 0 else "ago"
            mins = abs(int(delta))
            return (
                True,
                f"High-impact news: '{ev.get('title', '?')}' ({ev.get('country','?')}) "
                f"{'in' if delta >= 0 else ''} {mins} min {'ago' if delta < 0 else ''}".strip(),
            )

    return False, ""


def next_news_events(symbol: str, look_ahead_hours: int = 4) -> list[dict]:
    """Return upcoming high-impact events for a pair within the look-ahead window."""
    events = _get_events()
    now_utc = datetime.now(timezone.utc)
    cutoff  = now_utc + timedelta(hours=look_ahead_hours)
    currencies = PAIR_CURRENCIES.get(symbol, [])
    result = []
    for ev in events:
        if ev.get("impact", "").lower() != "high":
            continue
        if ev.get("country", "").upper() not in currencies:
            continue
        ev_time = _parse_event_time(ev.get("date", ""))
        if ev_time and now_utc <= ev_time <= cutoff:
            result.append({
                "title":   ev.get("title", "?"),
                "country": ev.get("country", "?"),
                "time":    ev_time,
                "minutes_away": int((ev_time - now_utc).total_seconds() / 60),
            })
    return sorted(result, key=lambda x: x["time"])


# ── Internals ─────────────────────────────────────────────────────────────

def _get_events() -> list[dict]:
    global _cache, _cache_ts
    age_minutes = (time.time() - _cache_ts) / 60

    if _cache and age_minutes < CACHE_MINUTES:
        return _cache

    try:
        resp = requests.get(CALENDAR_URL, timeout=8, headers={"User-Agent": "TradingBot/1.0"})
        resp.raise_for_status()
        _cache    = resp.json()
        _cache_ts = time.time()
        logger.info(f"Economic calendar refreshed — {len(_cache)} events loaded.")
    except Exception as e:
        logger.warning(f"Calendar fetch failed ({e}). Using cached/empty data.")
        if not _cache:
            return []

    return _cache


def _parse_event_time(date_str: str) -> datetime | None:
    """Parse ISO-8601 date string to UTC datetime."""
    if not date_str:
        return None
    try:
        # Handle both Z and offset formats
        date_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None
