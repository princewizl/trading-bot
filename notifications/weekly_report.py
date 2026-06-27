"""
Weekly performance report — sent every Sunday at 20:00 UTC.

Analyses the past 7 days of trades and emails:
  - Overall win rate, net PnL, balance
  - Win rate per currency pair (with ⚠ on under-performers)
  - Win rate per session (London, New York, Tokyo, Sydney)
  - Top failing checks on losing trades (shows strategy weak spots)
  - Actionable recommendations (pause poor pairs, lean into strong ones)
  - All-time cumulative stats
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def should_send_weekly() -> bool:
    """True on Sunday between 20:00–20:59 UTC."""
    now = datetime.now(timezone.utc)
    return now.weekday() == 6 and now.hour == 20


def send_weekly_report():
    from data.trade_journal import get_all_trades
    from notifications.alerts import _send, _email_configured

    if not _email_configured():
        logger.warning("Email not configured — skipping weekly report")
        return

    all_trades  = get_all_trades()
    week_trades = _last_n_days(all_trades, 7)

    if not week_trades:
        logger.info("No trades in last 7 days — skipping weekly report")
        return

    stats   = _compute_stats(week_trades, all_trades)
    subject = f"Weekly Trading Report — {datetime.now(timezone.utc).strftime('%d %b %Y')}"
    _send(subject, _build_html(stats), _build_plain(stats))
    logger.info(f"Weekly report sent: {len(week_trades)} trades in last 7 days")


# ── Internal ──────────────────────────────────────────────────────────────

def _last_n_days(trades: list[dict], n: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=n)
    result = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t.get("timestamp", ""))
            if ts >= cutoff:
                result.append(t)
        except ValueError:
            pass
    return result


def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.get("outcome") == "WIN") / len(trades)


def _net_pnl(trades: list[dict]) -> float:
    return sum(float(t.get("profit", 0)) for t in trades)


def _compute_stats(week: list[dict], all_trades: list[dict]) -> dict:
    # Per-pair
    pair_map = defaultdict(list)
    for t in week:
        pair_map[t.get("pair_display", t.get("pair", "?"))].append(t)

    pair_table = sorted([
        {
            "pair":   pair,
            "trades": len(rows),
            "wins":   sum(1 for r in rows if r.get("outcome") == "WIN"),
            "wr":     _win_rate(rows),
            "pnl":    _net_pnl(rows),
        }
        for pair, rows in pair_map.items()
    ], key=lambda x: x["wr"], reverse=True)

    # Per-session
    sess_map = defaultdict(list)
    for t in week:
        sess_map[t.get("session") or "Unknown"].append(t)

    sess_table = sorted([
        {"session": s, "trades": len(rows), "wr": _win_rate(rows), "pnl": _net_pnl(rows)}
        for s, rows in sess_map.items()
    ], key=lambda x: x["wr"], reverse=True)

    # Failing checks on losing trades — what was already passing but trade still lost
    losing = [t for t in week if t.get("outcome") == "LOSS"]
    fail_counts: dict[str, int] = defaultdict(int)
    for t in losing:
        for chk in t.get("checks_failed", "").split("|"):
            chk = chk.strip()
            if chk:
                fail_counts[chk] += 1
    top_failing = sorted(fail_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Confidence breakdown
    conf_map = defaultdict(list)
    for t in week:
        conf_map[t.get("confidence", "?")].append(t)
    conf_stats = {
        k: {"trades": len(v), "wr": _win_rate(v)}
        for k, v in conf_map.items()
    }

    latest_balance = 0.0
    currency       = "USD"
    if week:
        latest_balance = float(week[-1].get("balance_after", 0))
        currency       = week[-1].get("currency", "USD")

    return {
        "total_week":     len(week),
        "wins_week":      sum(1 for t in week if t.get("outcome") == "WIN"),
        "wr_week":        _win_rate(week),
        "pnl_week":       _net_pnl(week),
        "currency":       currency,
        "balance_latest": latest_balance,
        "pair_table":     pair_table,
        "sess_table":     sess_table,
        "top_failing":    top_failing,
        "conf_stats":     conf_stats,
        "total_all":      len(all_trades),
        "wr_all":         _win_rate(all_trades),
        "pnl_all":        _net_pnl(all_trades),
    }


def _build_html(s: dict) -> str:
    date_str  = datetime.now(timezone.utc).strftime("%d %B %Y")
    pnl_color = "#0f9d58" if s["pnl_week"] >= 0 else "#d93025"
    wr_color  = "#0f9d58" if s["wr_week"] >= 0.6 else ("#f4b400" if s["wr_week"] >= 0.5 else "#d93025")
    currency  = s["currency"]

    def pnl_c(v):
        return "#0f9d58" if float(v) >= 0 else "#d93025"

    def wr_c(v):
        return "#0f9d58" if v >= 0.6 else ("#f4b400" if v >= 0.5 else "#d93025")

    # Pair table
    pair_rows = ""
    for p in s["pair_table"]:
        flag = " &#9888;" if p["wr"] < 0.5 and p["trades"] >= 3 else ""
        pair_rows += f"""
      <tr>
        <td style="padding:9px 14px;border:1px solid #eee;">{p['pair']}{flag}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:center;">{p['trades']}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:center;">{p['wins']}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:center;
                   font-weight:bold;color:{wr_c(p['wr'])};">{p['wr']:.0%}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:right;
                   color:{pnl_c(p['pnl'])};">{'+' if p['pnl']>=0 else ''}{p['pnl']:.2f}</td>
      </tr>"""

    # Session table
    sess_rows = ""
    for s2 in s["sess_table"]:
        sess_rows += f"""
      <tr>
        <td style="padding:9px 14px;border:1px solid #eee;">{s2['session']}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:center;">{s2['trades']}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:center;
                   font-weight:bold;color:{wr_c(s2['wr'])};">{s2['wr']:.0%}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:right;
                   color:{pnl_c(s2['pnl'])};">{'+' if s2['pnl']>=0 else ''}{s2['pnl']:.2f}</td>
      </tr>"""

    # Failing checks
    if s["top_failing"]:
        fail_rows = "".join(f"""
      <tr>
        <td style="padding:9px 14px;border:1px solid #eee;">{chk}</td>
        <td style="padding:9px 14px;border:1px solid #eee;text-align:center;
                   color:#d93025;font-weight:bold;">{cnt}x</td>
      </tr>""" for chk, cnt in s["top_failing"])
    else:
        fail_rows = '<tr><td colspan="2" style="padding:9px 14px;border:1px solid #eee;color:#999;">No losing trades this week — excellent!</td></tr>'

    # Recommendations
    recs = []
    for p in s["pair_table"]:
        if p["wr"] < 0.5 and p["trades"] >= 3:
            recs.append(f'&#9888; Consider pausing <strong>{p["pair"]}</strong> — only {p["wr"]:.0%} win rate over {p["trades"]} trades.')
    for p in s["pair_table"]:
        if p["wr"] >= 0.7 and p["trades"] >= 3:
            recs.append(f'&#10003; <strong>{p["pair"]}</strong> is your best performer — {p["wr"]:.0%} win rate. Trade it more confidently.')
    if s["sess_table"]:
        worst_sess = min(s["sess_table"], key=lambda x: x["wr"])
        if worst_sess["wr"] < 0.5 and worst_sess["trades"] >= 3:
            recs.append(f'&#9888; The <strong>{worst_sess["session"]}</strong> session is underperforming ({worst_sess["wr"]:.0%}). Consider adding tighter filters during this window.')
    if not recs:
        recs.append("&#10003; Performance looks balanced across all pairs and sessions. No changes needed.")

    rec_html = "".join(f'<li style="margin:7px 0;line-height:1.6;">{r}</li>' for r in recs)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:Arial,sans-serif;">

  <div style="background:#1a73e8;padding:28px 20px;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:26px;">Weekly Trading Performance</h1>
    <p style="color:rgba(255,255,255,0.85);margin:8px 0 0;">{date_str}</p>
  </div>

  <!-- KEY METRICS -->
  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);padding:22px;text-align:center;">
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="padding:12px;border-right:1px solid #eee;">
          <div style="font-size:12px;color:#888;margin-bottom:4px;">TRADES</div>
          <div style="font-size:30px;font-weight:bold;color:#333;">{s['total_week']}</div>
          <div style="font-size:11px;color:#aaa;">{s['wins_week']} wins / {s['total_week']-s['wins_week']} losses</div>
        </td>
        <td style="padding:12px;border-right:1px solid #eee;">
          <div style="font-size:12px;color:#888;margin-bottom:4px;">WIN RATE</div>
          <div style="font-size:30px;font-weight:bold;color:{wr_color};">{s['wr_week']:.0%}</div>
        </td>
        <td style="padding:12px;border-right:1px solid #eee;">
          <div style="font-size:12px;color:#888;margin-bottom:4px;">NET PnL</div>
          <div style="font-size:30px;font-weight:bold;color:{pnl_color};">
            {'+' if s['pnl_week']>=0 else ''}{s['pnl_week']:.2f}
          </div>
        </td>
        <td style="padding:12px;">
          <div style="font-size:12px;color:#888;margin-bottom:4px;">BALANCE</div>
          <div style="font-size:24px;font-weight:bold;color:#333;">{currency}<br>{s['balance_latest']:.2f}</div>
        </td>
      </tr>
    </table>
  </div>

  <!-- PAIR BREAKDOWN -->
  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:14px 20px;border-bottom:2px solid #1a73e8;background:#f8f9fa;">
      <h3 style="margin:0;color:#333;">Performance by Pair</h3>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <tr style="background:#f8f9fa;font-size:12px;color:#666;">
        <th style="padding:9px 14px;border:1px solid #eee;text-align:left;">Pair</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Trades</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Wins</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Win %</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Net PnL</th>
      </tr>
      {pair_rows}
    </table>
    <div style="padding:8px 16px;font-size:11px;color:#999;">
      &#9888; = pairs with below 50% win rate (3+ trades). Consider pausing these.
    </div>
  </div>

  <!-- SESSION BREAKDOWN -->
  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:14px 20px;border-bottom:2px solid #1a73e8;background:#f8f9fa;">
      <h3 style="margin:0;color:#333;">Performance by Session</h3>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <tr style="background:#f8f9fa;font-size:12px;color:#666;">
        <th style="padding:9px 14px;border:1px solid #eee;text-align:left;">Session</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Trades</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Win %</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Net PnL</th>
      </tr>
      {sess_rows}
    </table>
  </div>

  <!-- FAILING CHECKS ANALYSIS -->
  <div style="max-width:600px;margin:16px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 6px rgba(0,0,0,0.1);overflow:hidden;">
    <div style="padding:14px 20px;border-bottom:2px solid #f4b400;background:#f8f9fa;">
      <h3 style="margin:0;color:#333;">Strategy Weak Spots</h3>
      <p style="margin:4px 0 0;font-size:12px;color:#888;">
        Checks that failed most often on <strong>losing trades</strong>.
        High counts = this check is not filtering bad trades effectively.
      </p>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <tr style="background:#f8f9fa;font-size:12px;color:#666;">
        <th style="padding:9px 14px;border:1px solid #eee;text-align:left;">Confluence Check</th>
        <th style="padding:9px 14px;border:1px solid #eee;">Times Failed on Losses</th>
      </tr>
      {fail_rows}
    </table>
  </div>

  <!-- RECOMMENDATIONS -->
  <div style="max-width:600px;margin:16px auto;background:#e8f5e9;border-radius:8px;
              border-left:5px solid #0f9d58;padding:18px 22px;">
    <h3 style="margin:0 0 12px;color:#1b5e20;font-size:16px;">Recommendations for Next Week</h3>
    <ul style="margin:0;padding-left:22px;color:#333;font-size:14px;">
      {rec_html}
    </ul>
  </div>

  <!-- ALL-TIME -->
  <div style="max-width:600px;margin:16px auto 32px;background:#f8f9fa;border-radius:8px;
              padding:14px 20px;font-size:13px;color:#666;text-align:center;">
    <strong>All-time:</strong> &nbsp;
    {s['total_all']} trades &nbsp;|&nbsp;
    Win rate: <strong style="color:{wr_c(s['wr_all'])};">{s['wr_all']:.0%}</strong> &nbsp;|&nbsp;
    Net PnL: <strong style="color:{pnl_c(s['pnl_all'])}">{'+' if s['pnl_all']>=0 else ''}{s['pnl_all']:.2f}</strong>
  </div>

</body>
</html>"""


def _build_plain(s: dict) -> str:
    lines = [
        f"WEEKLY TRADING REPORT — {datetime.now(timezone.utc).strftime('%d %b %Y')}",
        f"",
        f"This week: {s['total_week']} trades | Win rate: {s['wr_week']:.0%} | Net PnL: {s['pnl_week']:+.2f}",
        f"Balance: {s['currency']} {s['balance_latest']:.2f}",
        f"",
        "BY PAIR:",
    ]
    for p in s["pair_table"]:
        flag = " [UNDERPERFORMING]" if p["wr"] < 0.5 and p["trades"] >= 3 else ""
        lines.append(f"  {p['pair']:<12} {p['trades']} trades  {p['wr']:.0%} win  PnL: {p['pnl']:+.2f}{flag}")
    lines += ["", "BY SESSION:"]
    for s2 in s["sess_table"]:
        lines.append(f"  {s2['session']:<14} {s2['trades']} trades  {s2['wr']:.0%} win  PnL: {s2['pnl']:+.2f}")
    if s["top_failing"]:
        lines += ["", "STRATEGY WEAK SPOTS (failed checks on losing trades):"]
        for chk, cnt in s["top_failing"]:
            lines.append(f"  {cnt}x  {chk}")
    lines += [
        "",
        f"ALL-TIME: {s['total_all']} trades | {s['wr_all']:.0%} win | PnL: {s['pnl_all']:+.2f}",
    ]
    return "\n".join(lines)
