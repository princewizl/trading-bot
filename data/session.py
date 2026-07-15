"""
Trading session filter.

Forex liquidity is NOT uniform. Each pair has a peak window where volume,
trend reliability, and spread conditions are best. Signals outside these
windows have lower win rates and wider spreads.

All times are UTC. Sessions defined conservatively — we skip the first
15 minutes of any session opening (chaotic, spreads wide) and last
15 minutes before close (liquidity drops off).

Sessions:
  Sydney  : 21:00 – 06:00 UTC
  Tokyo   : 00:00 – 09:00 UTC
  London  : 07:00 – 16:00 UTC
  New York: 12:00 – 21:00 UTC

Overlap windows (highest liquidity / most reliable trends):
  Tokyo/London: 07:15 – 08:45 UTC
  London/NY   : 12:00 – 16:00 UTC
"""

from datetime import datetime, timezone, time as dtime

# Each entry: (open_utc_hhmm, close_utc_hhmm)
# Skip 15 min after open and 15 min before close of each raw session
SESSIONS = {
    "LONDON": (dtime(7, 15),  dtime(15, 45)),
    "NEW_YORK": (dtime(12, 15), dtime(20, 45)),
    "TOKYO": (dtime(0, 15),   dtime(8, 45)),
    "SYDNEY": (dtime(21, 15), dtime(5, 45)),    # wraps midnight
}

# Global no-trade windows (UTC). Pairs in session but in these windows still get
# signal emails — we just skip auto-placement and log the reason.
# Data-driven from 113-trade analysis (Jun 29 – Jul 15):
#   07:00-08:15 → London open chaos:      12 trades, 41.7% WR, -$1,354
#   13:00-14:00 → London/NY handover:     25 trades, 48.0% WR,   -$712
BLACKOUT_WINDOWS: list[tuple[dtime, dtime]] = [
    (dtime(7, 0),  dtime(8, 15)),   # London open volatility
    (dtime(13, 0), dtime(14, 0)),   # London/NY overlap handover
]

# Optimal sessions per pair. A signal must fall in at least one of these.
PAIR_SESSIONS: dict[str, list[str]] = {
    "EURUSD=X": ["LONDON", "NEW_YORK"],
    "GBPUSD=X": ["LONDON", "NEW_YORK"],
    "USDJPY=X": ["TOKYO", "NEW_YORK"],
    "AUDUSD=X": ["SYDNEY", "TOKYO", "NEW_YORK"],
    "USDCAD=X": ["NEW_YORK"],
    "EURGBP=X": ["LONDON"],
    "GBPJPY=X": ["LONDON"],         # most liquid and trending during London only
}


# ── Public API ────────────────────────────────────────────────────────────

def is_session_active(symbol: str) -> tuple[bool, str]:
    """
    Returns (True, session_name) if we are inside an optimal trading window
    for this pair. Returns (False, reason) if outside all optimal sessions.
    """
    now_utc = datetime.now(timezone.utc).time().replace(second=0, microsecond=0)

    # Global blackout windows override session membership
    for bstart, bend in BLACKOUT_WINDOWS:
        if _time_in_range(now_utc, bstart, bend):
            return False, f"blackout {bstart.strftime('%H:%M')}–{bend.strftime('%H:%M')} UTC (high-volatility window)"

    allowed = PAIR_SESSIONS.get(symbol, list(SESSIONS.keys()))

    for session_name in allowed:
        window = SESSIONS.get(session_name)
        if window and _time_in_range(now_utc, window[0], window[1]):
            return True, session_name

    # Build readable reason
    windows = [
        f"{SESSIONS[s][0].strftime('%H:%M')}–{SESSIONS[s][1].strftime('%H:%M')} UTC ({s})"
        for s in allowed if s in SESSIONS
    ]
    return False, f"Outside optimal session(s): {', '.join(windows)}"


def active_sessions_now() -> list[str]:
    """Return all session names that are currently active."""
    now_utc = datetime.now(timezone.utc).time().replace(second=0, microsecond=0)
    return [
        name for name, (open_, close_) in SESSIONS.items()
        if _time_in_range(now_utc, open_, close_)
    ]


def minutes_to_next_session(symbol: str) -> int | None:
    """
    Return how many minutes until the next optimal session for a pair opens.
    Returns None if no next session found within 24 hours.
    """
    from datetime import date, timedelta, datetime as dt
    now_utc = datetime.now(timezone.utc)
    now_t   = now_utc.time()
    allowed = PAIR_SESSIONS.get(symbol, list(SESSIONS.keys()))
    min_wait = None

    for session_name in allowed:
        if session_name not in SESSIONS:
            continue
        open_t = SESSIONS[session_name][0]
        # Next occurrence of open_t
        today = now_utc.date()
        candidate = dt.combine(today, open_t, tzinfo=timezone.utc)
        if candidate <= now_utc:
            candidate += timedelta(days=1)
        wait = int((candidate - now_utc).total_seconds() / 60)
        if min_wait is None or wait < min_wait:
            min_wait = wait

    return min_wait


# ── Internals ─────────────────────────────────────────────────────────────

def _time_in_range(t: dtime, start: dtime, end: dtime) -> bool:
    """Check if time t is in [start, end], handling midnight wrap."""
    if start <= end:
        return start <= t <= end
    # Wraps midnight (e.g. Sydney 21:15 – 05:45)
    return t >= start or t <= end
