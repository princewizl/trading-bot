"""
Economic calendar filter.

Fetches the current week's high-impact news events from a free ForexFactory
feed and blocks trading on any pair whose currencies have an event within
the configured buffer window.

Data source: https://nfs.faireconomy.media/ff_calendar_thisweek.json
Cached in memory for 60 minutes so we don't spam the endpoint.
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
CACHE_MINUTES  = 60   # re-fetch from API every 60 minutes

# File-based cache so the data survives between run_scan.py invocations
_CACHE_FILE = Path("data/.calendar_cache.json")

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

    # 1. In-memory cache (within the same process)
    if _cache and (time.time() - _cache_ts) / 60 < CACHE_MINUTES:
        return _cache

    # 2. File-based cache (survives between run_scan.py invocations)
    if _load_file_cache():
        return _cache

    # 3. Fetch from API
    try:
        resp = requests.get(
            CALENDAR_URL, timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"}
        )
        resp.raise_for_status()
        _cache    = resp.json()
        _cache_ts = time.time()
        _save_file_cache()
        logger.info(f"Economic calendar refreshed — {len(_cache)} events loaded.")
    except Exception as e:
        # On failure: mark cache_ts so we don't immediately retry (rate-limit backoff)
        _cache_ts = time.time()
        logger.warning(f"Calendar fetch failed ({e}). Using cached/empty data.")

    return _cache


def _load_file_cache() -> bool:
    """Load cache from disk. Returns True if fresh data was found."""
    global _cache, _cache_ts
    try:
        if not _CACHE_FILE.exists():
            return False
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        saved_at = float(data.get("saved_at", 0))
        age_minutes = (time.time() - saved_at) / 60
        if age_minutes < CACHE_MINUTES and data.get("events"):
            _cache    = data["events"]
            _cache_ts = saved_at
            logger.debug(f"Calendar loaded from file cache ({age_minutes:.0f} min old, {len(_cache)} events)")
            return True
    except Exception as e:
        logger.debug(f"Calendar file cache unreadable: {e}")
    return False


def _save_file_cache():
    """Persist current cache to disk for next run."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"saved_at": _cache_ts, "events": _cache}),
            encoding="utf-8"
        )
    except Exception as e:
        logger.debug(f"Calendar file cache write failed: {e}")


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
