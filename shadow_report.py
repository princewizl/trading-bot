"""
Shadow-log report — replays filter combinations over every logged setup and
shows win rate, its 95% confidence interval, trades/day and expected value at
each expiry. Use it to decide which gates to keep BEFORE changing live config.

    python shadow_report.py                 # all data
    python shadow_report.py --days 14       # last 14 days only
    python shadow_report.py --payout 0.82   # override payout used for EV / break-even

On the server:  docker compose exec bot python shadow_report.py

Reading it: a rule set is only better if its CI sits clearly above break-even,
not just its point estimate. With under ~200 resolved trades the CI is
usually too wide to conclude anything — keep collecting.
"""

import argparse
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
from data import db

HORIZONS = (5, 15, 30)

# Index of each check in Signal check order (strategy/trend_following.py).
C_HTF, C_EMA50, C_CROSS, C_ALIGN, C_ADX, C_DI, C_RSI, C_PATTERN, C_MACD, C_BB = range(10)


# ── Rule replay ───────────────────────────────────────────────────────────

def live_rules(**overrides):
    """Predicate replicating the live gates; overrides switch individual gates."""
    o = dict(
        min_passed=round(config.MIN_SIGNAL_STRENGTH * 10),
        adx_min=config.ADX_HARD_MIN, adx_max=config.ADX_HARD_MAX,
        htf_gate=True, rsi_gate=config.RSI_HARD_GATE,
        session=True, blackout=True, news=True,
        direction=dict(config.PAIR_DIRECTION_RESTRICT),
    )
    o.update(overrides)

    def ok(r):
        if r["num_passed"] < o["min_passed"]:
            return False
        if o["adx_min"] is not None and r["adx"] < o["adx_min"]:
            return False
        if o["adx_max"] is not None and r["adx"] >= o["adx_max"]:
            return False
        if o["htf_gate"] and ((r["direction"] == "BUY" and r["htf_trend"] == "DOWN")
                              or (r["direction"] == "SELL" and r["htf_trend"] == "UP")):
            return False
        if o["rsi_gate"] and ((r["direction"] == "BUY" and r["rsi"] >= config.RSI_OVERBOUGHT)
                              or (r["direction"] == "SELL" and r["rsi"] <= config.RSI_OVERSOLD)):
            return False
        if o["session"] and not r["session"]:
            return False
        if o["blackout"] and r["blackout"]:
            return False
        if o["news"] and r["news_blocked"]:
            return False
        allowed = o["direction"].get(r["pair"])
        if allowed and r["direction"] != allowed:
            return False
        return True
    return ok


SCENARIOS = [
    ("LIVE RULES (current config)",            {}),
    ("min score 7/10",                         {"min_passed": 7}),
    ("min score 6/10",                         {"min_passed": 6}),
    ("min score 9/10",                         {"min_passed": 9}),
    ("no ADX floor",                           {"adx_min": None}),
    ("ADX floor 30",                           {"adx_min": 30}),
    ("+ ADX ceiling 50",                       {"adx_max": 50}),
    ("+ RSI hard gate",                        {"rsi_gate": True}),
    ("no H1 counter-trend gate",               {"htf_gate": False}),
    ("no session filter",                      {"session": False}),
    ("no blackout windows",                    {"blackout": False}),
    ("no news filter",                         {"news": False}),
    ("no pair direction locks",                {"direction": {}}),
    ("+ GBP/USD SELL-only",                    {"direction": {**config.PAIR_DIRECTION_RESTRICT, "GBPUSD=X": "SELL"}}),
]


# ── Stats ─────────────────────────────────────────────────────────────────

def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def summarise(rows, h, payout, days):
    res = [r[f"res_{h}"] for r in rows if r[f"res_{h}"] in ("WIN", "LOSS")]
    n, w = len(res), res.count("WIN")
    if n == 0:
        return None
    p = w / n
    lo, hi = wilson(w, n)
    return {"n": n, "per_day": n / days, "wr": p, "lo": lo, "hi": hi, "ev": p * payout - (1 - p)}


def fmt(s, be):
    if s is None:
        return "   n=0"
    flag = "  ✓ CI above BE" if s["lo"] > be else ("  ✗ CI below BE" if s["hi"] < be else "")
    return (f"n={s['n']:>4}  {s['per_day']:>5.1f}/day  WR {s['wr']:6.1%}  "
            f"CI [{s['lo']:5.1%}–{s['hi']:5.1%}]  EV {s['ev']:+.3f}/$1{flag}")


def breakdown(rows, key, h, payout, days, be, title, min_n=10):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    print(f"\n  by {title}:")
    for k in sorted(groups, key=str):
        s = summarise(groups[k], h, payout, days)
        if s and s["n"] >= min_n:
            print(f"    {str(k):<24} {fmt(s, be)}")
    small = sum(1 for k in groups if (summarise(groups[k], h, payout, days) or {"n": 0})["n"] < min_n)
    if small:
        print(f"    ({small} group(s) with < {min_n} resolved rows hidden)")


def shadow_vs_real(rows):
    """Check the shadow outcome method against real IQ results for traded rows."""
    try:
        with db.connect() as c:
            trades = [dict(t) for t in c.execute("SELECT * FROM trades WHERE status IN ('WIN','LOSS')")]
    except sqlite3.Error:
        return
    agree = total = 0
    for r in rows:
        if not r["traded"] or r["res_5"] not in ("WIN", "LOSS"):
            continue
        ts = datetime.fromisoformat(r["ts"])
        for t in trades:
            if (t["pair"] == r["pair"] and t["direction"] == r["direction"]
                    and abs((datetime.fromisoformat(t["placed_at"]) - ts).total_seconds()) < 120):
                total += 1
                agree += (t["status"] == r["res_5"])
                break
    if total:
        print(f"\nShadow 5m outcome vs real IQ result on {total} traded setups: {agree / total:.0%} agree")


# ── Main ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--payout", type=float, default=0.0)
    ap.add_argument("--horizon", type=int, default=5, choices=HORIZONS, help="expiry for breakdowns")
    args = ap.parse_args(argv)

    rows = db.shadow_all()
    if args.days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        rows = [r for r in rows if r["ts"] >= cutoff]
    if not rows:
        print("No shadow rows yet.")
        return

    payout = args.payout
    if not payout:
        with db.connect() as c:
            p = [r[0] for r in c.execute("SELECT payout FROM trades WHERE payout IS NOT NULL")]
        payout = statistics.median(p) if p else 0.84
    be = 1 / (1 + payout)
    days = max(1, len({r["ts"][:10] for r in rows}))

    print(f"Shadow log: {len(rows)} setups (>= 6/10) over {days} trading day(s), "
          f"{min(r['ts'] for r in rows)[:16]} → {max(r['ts'] for r in rows)[:16]} UTC")
    print(f"Payout {payout:.0%} → break-even WR {be:.1%}")

    for h in HORIZONS:
        print(f"\n═══ {h}-minute expiry ═══")
        for label, ov in SCENARIOS:
            sel = [r for r in rows if live_rules(**ov)(r)]
            print(f"  {label:<30} {fmt(summarise(sel, h, payout, days), be)}")

    h = args.horizon
    live = [r for r in rows if live_rules()(r)]
    print(f"\n═══ Breakdowns under LIVE RULES, {h}-minute expiry ═══")
    breakdown(live, lambda r: f"{config.PAIR_DISPLAY.get(r['pair'], r['pair'])} {r['direction']}", h, payout, days, be, "pair + direction")
    breakdown(live, lambda r: r["session"] or "off-peak", h, payout, days, be, "session")
    breakdown(live, lambda r: f"{r['ts'][11:13]}:00", h, payout, days, be, "hour (UTC)")
    breakdown(live, lambda r: f"ADX {int(r['adx'] // 5 * 5):>2}-{int(r['adx'] // 5 * 5) + 5}", h, payout, days, be, "ADX")
    breakdown(live, lambda r: f"RSI {int(r['rsi'] // 10 * 10):>2}s", h, payout, days, be, "RSI")
    breakdown(live, lambda r: r["pattern"] or "none", h, payout, days, be, "candle pattern")
    breakdown(live, lambda r: f"{r['num_passed']}/10", h, payout, days, be, "score")

    all_scored = [r for r in rows if live_rules(min_passed=6)(r)]
    names = ["H1 aligned", "EMA50 side", "EMA cross", "EMA aligned", "ADX>=33", "DI bias",
             "RSI zone", "candle pattern", "MACD", "BB breakout"]
    print(f"\n═══ Each check's own predictive value (live gates, score >= 6), {h}-minute expiry ═══")
    for i, nm in enumerate(names):
        on  = summarise([r for r in all_scored if r["checks"][i] == "1"], h, payout, days)
        off = summarise([r for r in all_scored if r["checks"][i] == "0"], h, payout, days)
        print(f"  {nm:<15} passed: {fmt(on, be)}")
        print(f"  {'':<15} failed: {fmt(off, be)}")

    shadow_vs_real(rows)


if __name__ == "__main__":
    main()
