"""
One-time import of the old Gist trade journal (trade_log.csv) into SQLite, so
the PWA shows the full history and the Gist / GIST_TOKEN can be retired.

Secret gists are readable by their raw URL, so no token is needed:

    python import_history.py                      # uses TRADE_LOG_GIST_ID from .env
    python import_history.py --file trade_log.csv # or a downloaded copy

Safe to re-run: rows are keyed by contract id and never duplicated. PENDING
markers are skipped. Rows are tagged result_source='legacy' — note that results
before Jul 20 2026 came from the old balance-diff method, which mislabeled some
losses as wins; the balance column is the trustworthy number for that period.
"""

import argparse
import csv
import io
import os

import requests
from dotenv import load_dotenv

load_dotenv()

from data import db  # noqa: E402  (BOT_DB_PATH may come from .env)


def _f(v):
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--gist-id", default=os.getenv("TRADE_LOG_GIST_ID", ""))
    ap.add_argument("--user", default="princewizl")
    args = ap.parse_args()

    if args.file:
        text = open(args.file, encoding="utf-8").read()
    else:
        if not args.gist_id:
            raise SystemExit("Pass --file or set TRADE_LOG_GIST_ID")
        url = f"https://gist.githubusercontent.com/{args.user}/{args.gist_id}/raw/trade_log.csv"
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        text = r.text

    db.init()
    added = skipped = 0
    for i, row in enumerate(csv.DictReader(io.StringIO(text))):
        outcome = (row.get("outcome") or "").upper()
        cid = (row.get("contract_id") or "").strip()
        if outcome not in ("WIN", "LOSS", "TIE", "UNKNOWN") or cid.startswith("PENDING"):
            skipped += 1
            continue
        stake, profit = _f(row.get("stake")) or 0.0, _f(row.get("profit"))
        strength = _f(row.get("strength_pct"))
        with db.connect() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO trades
                   (order_id, placed_at, expires_at, pair, ticker, direction, session, confidence,
                    strength, num_passed, checks_passed, checks_failed, price, adx, rsi, macd_hist,
                    bb_width, htf_trend, pattern, stake, payout, currency, status, profit,
                    balance_after, result_source, settled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cid or f"legacy-{i}", row["timestamp"], row["timestamp"], row["pair"],
                    row["pair"].replace("=X", ""), row.get("direction", ""), row.get("session", ""),
                    row.get("confidence", ""), strength / 100 if strength is not None else None,
                    int(row["num_passed"]) if (row.get("num_passed") or "").isdigit() else None,
                    row.get("checks_passed", ""), row.get("checks_failed", ""), _f(row.get("entry_price")),
                    _f(row.get("adx")), _f(row.get("rsi")), _f(row.get("macd_hist")),
                    (_f(row.get("bb_width_pct")) or 0) / 100, row.get("htf_trend", ""), row.get("pattern", ""),
                    stake, (profit / stake) if (outcome == "WIN" and stake and profit) else None,
                    row.get("currency") or "USD", outcome, profit, _f(row.get("balance_after")),
                    "legacy", row["timestamp"],
                ),
            )
            added += cur.rowcount
    print(f"Imported {added} trade(s); {skipped} pending/invalid row(s) skipped; duplicates ignored.")


if __name__ == "__main__":
    main()
