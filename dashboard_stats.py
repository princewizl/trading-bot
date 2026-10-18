"""
Dashboard statistics over journal-shaped trade rows (data.db.journal_rows()).
Shared by the PWA (web_app.py).
"""

import os
import statistics
from datetime import datetime, timezone

START_BALANCE = float(os.environ.get("START_BALANCE", "0") or "0")
BREAK_EVEN_WR = 0.588   # fallback (70% payout) until the journal has wins


# ── Stats computation ──────────────────────────────────────────────────────────
def _f(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def compute_stats(rows: list[dict]) -> dict:
    completed = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]
    if not completed:
        return _empty_stats()

    # Break-even from the payouts actually received: WR_be = 1 / (1 + payout)
    payouts = [_f(r.get("profit")) / _f(r.get("stake")) for r in completed
               if r["outcome"] == "WIN" and _f(r.get("stake")) > 0 and _f(r.get("profit")) > 0]
    # Median, not mean: the earliest balance-diff rows recorded "profits" of up to 4x stake.
    avg_payout = statistics.median(payouts) if payouts else 1 / BREAK_EVEN_WR - 1
    break_even = 1 / (1 + avg_payout)

    # Equity curve (skip rows where balance_after is 0 — concurrent-scan artefacts)
    eq_points = [
        {"ts": r["timestamp"], "bal": round(_f(r["balance_after"]), 2)}
        for r in completed
        if _f(r.get("balance_after")) > 0
    ]
    eq_bal = [p["bal"] for p in eq_points]
    eq_ts  = [p["ts"]  for p in eq_points]

    current_bal  = eq_bal[-1] if eq_bal else 0.0
    peak_bal     = max(eq_bal) if eq_bal else 0.0
    low_bal      = min(eq_bal) if eq_bal else 0.0
    start_bal    = START_BALANCE if START_BALANCE > 0 else (eq_bal[0] if eq_bal else 0.0)
    net_pnl      = round(current_bal - start_bal, 2) if start_bal else None
    draw_now_pct = round((peak_bal - current_bal) / peak_bal * 100, 1) if peak_bal else 0.0
    draw_max_pct = round((peak_bal - low_bal)     / peak_bal * 100, 1) if peak_bal else 0.0

    # Per-pair
    pair_stats: dict[str, dict] = {}
    for r in completed:
        name = r.get("pair_display") or r.get("pair") or "?"
        ps = pair_stats.setdefault(name, {"wins": 0, "losses": 0, "profit_sum": 0.0})
        ps["profit_sum"] += _f(r.get("profit"))
        if r["outcome"] == "WIN":
            ps["wins"]   += 1
        else:
            ps["losses"] += 1
    for name, ps in pair_stats.items():
        t = ps["wins"] + ps["losses"]
        ps["trades"]     = t
        ps["wr"]         = round(ps["wins"] / t * 100, 1) if t else 0.0
        ps["net_profit"] = round(ps["profit_sum"], 2)
        ps["below_be"]   = ps["wr"] < (break_even * 100)
    pair_stats = dict(sorted(pair_stats.items(), key=lambda x: x[1]["wr"], reverse=True))

    # Daily
    daily: dict[str, dict] = {}
    for r in completed:
        day = r["timestamp"][:10]
        ds = daily.setdefault(day, {"wins": 0, "losses": 0, "profit_sum": 0.0})
        ds["profit_sum"] += _f(r.get("profit"))
        if r["outcome"] == "WIN":
            ds["wins"]   += 1
        else:
            ds["losses"] += 1
    for ds in daily.values():
        ds["trades"] = ds["wins"] + ds["losses"]
        ds["profit"] = round(ds["profit_sum"], 2)
        ds["wr"]     = round(ds["wins"] / ds["trades"] * 100, 1) if ds["trades"] else 0.0
    daily = dict(sorted(daily.items()))

    # Session
    session_stats: dict[str, dict] = {}
    for r in completed:
        sess = (r.get("session") or "unknown").strip() or "unknown"
        ss = session_stats.setdefault(sess, {"wins": 0, "losses": 0})
        if r["outcome"] == "WIN":
            ss["wins"]   += 1
        else:
            ss["losses"] += 1
    for ss in session_stats.values():
        t = ss["wins"] + ss["losses"]
        ss["trades"] = t
        ss["wr"]     = round(ss["wins"] / t * 100, 1) if t else 0.0

    # Confidence
    conf_stats: dict[str, dict] = {}
    for r in completed:
        conf = (r.get("confidence") or "?").strip() or "?"
        cs = conf_stats.setdefault(conf, {"wins": 0, "losses": 0})
        if r["outcome"] == "WIN":
            cs["wins"]   += 1
        else:
            cs["losses"] += 1
    for cs in conf_stats.values():
        t = cs["wins"] + cs["losses"]
        cs["trades"] = t
        cs["wr"]     = round(cs["wins"] / t * 100, 1) if t else 0.0

    # Direction
    dir_stats: dict[str, dict] = {}
    for r in completed:
        d = (r.get("direction") or "?").upper().strip() or "?"
        ds2 = dir_stats.setdefault(d, {"wins": 0, "losses": 0})
        if r["outcome"] == "WIN":
            ds2["wins"]   += 1
        else:
            ds2["losses"] += 1
    for ds2 in dir_stats.values():
        t = ds2["wins"] + ds2["losses"]
        ds2["trades"] = t
        ds2["wr"]     = round(ds2["wins"] / t * 100, 1) if t else 0.0

    # Streak
    streak, streak_type = 0, None
    for r in reversed(completed):
        out = r["outcome"]
        if streak_type is None:
            streak_type = out
        if out == streak_type:
            streak += 1
        else:
            break

    # Today
    today_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_rows = [r for r in completed if r["timestamp"].startswith(today_str)]
    today_wins = sum(1 for r in today_rows if r["outcome"] == "WIN")
    today_wr   = round(today_wins / len(today_rows) * 100, 1) if today_rows else 0.0

    # Recent 50 trades, newest first
    recent = [
        {
            "ts":         r["timestamp"][:16].replace("T", " ") + " UTC",
            "pair":       r.get("pair_display") or r.get("pair") or "?",
            "direction":  r.get("direction", ""),
            "confidence": r.get("confidence", ""),
            "strength":   r.get("strength_pct", ""),
            "adx":        r.get("adx", ""),
            "rsi":        r.get("rsi", ""),
            "session":    r.get("session", ""),
            "stake":      _f(r.get("stake")),
            "outcome":    r["outcome"],
            "profit":     _f(r.get("profit")),
            "balance":    _f(r.get("balance_after")),
            "pattern":    r.get("pattern", ""),
            "htf_trend":  r.get("htf_trend", ""),
        }
        for r in reversed(completed[-50:])
    ]

    total  = len(completed)
    wins   = sum(1 for r in completed if r["outcome"] == "WIN")

    return {
        "total_trades":     total,
        "wins":             wins,
        "losses":           total - wins,
        "overall_wr":       round(wins / total * 100, 1) if total else 0.0,
        "current_balance":  current_bal,
        "peak_balance":     round(peak_bal, 2),
        "low_balance":      round(low_bal, 2),
        "start_balance":    round(start_bal, 2),
        "net_pnl":          net_pnl,
        "drawdown_now_pct": draw_now_pct,
        "drawdown_max_pct": draw_max_pct,
        "streak":           streak,
        "streak_type":      streak_type,
        "today_trades":     len(today_rows),
        "today_wins":       today_wins,
        "today_losses":     len(today_rows) - today_wins,
        "today_wr":         today_wr,
        "pair_stats":       pair_stats,
        "daily":            daily,
        "session_stats":    session_stats,
        "conf_stats":       conf_stats,
        "dir_stats":        dir_stats,
        "equity_ts":        eq_ts,
        "equity_bal":       eq_bal,
        "recent":           recent,
        "break_even_wr":    round(break_even * 100, 1),
        "avg_payout_pct":   round(avg_payout * 100, 1),
    }


def _empty_stats() -> dict:
    return {
        "total_trades": 0, "wins": 0, "losses": 0, "overall_wr": 0.0,
        "current_balance": 0.0, "peak_balance": 0.0, "low_balance": 0.0,
        "start_balance": START_BALANCE, "net_pnl": None,
        "drawdown_now_pct": 0.0, "drawdown_max_pct": 0.0,
        "streak": 0, "streak_type": None,
        "today_trades": 0, "today_wins": 0, "today_losses": 0, "today_wr": 0.0,
        "pair_stats": {}, "daily": {}, "session_stats": {}, "conf_stats": {},
        "dir_stats": {}, "equity_ts": [], "equity_bal": [], "recent": [],
        "break_even_wr": round(BREAK_EVEN_WR * 100, 1),
        "avg_payout_pct": round((1 / BREAK_EVEN_WR - 1) * 100, 1),
    }
