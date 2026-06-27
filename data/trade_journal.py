"""
Trade Journal — persistent log of every auto-placed trade.

Storage options (tried in order):
  1. GitHub Gist (recommended for GitHub Actions — survives between runs)
  2. Local CSV file (works on Oracle Cloud / local machine)

One-time Gist setup:
  1. Go to gist.github.com → New Gist
  2. Filename: trade_log.csv  |  Content: (leave blank)  |  Secret Gist
  3. Click "Create secret gist"
  4. Copy the Gist ID from the URL: gist.github.com/yourusername/<GIST_ID>
  5. Add to .env and GitHub Secrets:
       TRADE_LOG_GIST_ID=the_id_you_copied
       GIST_TOKEN=your_github_pat  (needs "gist" scope)
"""

import csv
import io
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GIST_ID    = os.getenv("TRADE_LOG_GIST_ID", "")
GIST_TOKEN = os.getenv("GIST_TOKEN", os.getenv("GITHUB_TOKEN", ""))
GIST_FILE  = "trade_log.csv"
LOCAL_FILE = Path("trade_log.csv")

FIELDS = [
    "timestamp", "pair", "pair_display", "direction", "session",
    "confidence", "strength_pct", "adx", "rsi", "macd_hist", "bb_width_pct",
    "htf_trend", "pattern", "num_passed",
    "checks_passed", "checks_failed",
    "stake", "currency", "outcome", "profit", "balance_after",
    "entry_price", "exit_price", "contract_id",
]


# ── Public API ────────────────────────────────────────────────────────────

def log_trade(signal, result: dict, stake: float, session: str = "") -> bool:
    """Append one trade result to the journal. Returns True on success."""
    row = _build_row(signal, result, stake, session)
    if GIST_ID and GIST_TOKEN:
        ok = _gist_append(row)
        if ok:
            return True
        logger.warning("Gist write failed — falling back to local CSV")
    return _local_append(row)


def get_all_trades() -> list[dict]:
    """Read all trade records. Returns list of dicts (one per trade)."""
    if GIST_ID and GIST_TOKEN:
        rows = _gist_read()
        if rows is not None:
            return rows
    if LOCAL_FILE.exists():
        return _local_read()
    return []


def is_configured() -> bool:
    return bool((GIST_ID and GIST_TOKEN) or True)   # local always available


# ── Row builder ───────────────────────────────────────────────────────────

def _build_row(signal, result: dict, stake: float, session: str) -> dict:
    from config import PAIR_DISPLAY
    return {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "pair":          signal.symbol,
        "pair_display":  PAIR_DISPLAY.get(signal.symbol, signal.symbol),
        "direction":     signal.direction,
        "session":       session,
        "confidence":    signal.confidence_label,
        "strength_pct":  f"{signal.strength:.2%}",
        "adx":           f"{signal.adx:.2f}",
        "rsi":           f"{signal.rsi:.2f}",
        "macd_hist":     f"{signal.macd_hist:.8f}",
        "bb_width_pct":  f"{signal.bb_width * 100:.4f}",
        "htf_trend":     signal.htf_trend,
        "pattern":       signal.candlestick_pattern or "",
        "num_passed":    len(signal.checks_passed),
        "checks_passed": "|".join(signal.checks_passed),
        "checks_failed": "|".join(signal.checks_failed),
        "stake":         f"{stake:.2f}",
        "currency":      result.get("currency", "USD"),
        "outcome":       result.get("outcome", "UNKNOWN"),
        "profit":        f"{result.get('profit', 0):.2f}",
        "balance_after": f"{result.get('balance', 0):.2f}",
        "entry_price":   f"{result.get('entry_spot', signal.price):.5f}",
        "exit_price":    f"{result.get('exit_spot', 0):.5f}",
        "contract_id":   str(result.get("contract_id", "")),
    }


# ── Gist persistence ──────────────────────────────────────────────────────

def _gist_headers() -> dict:
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
    }


def _gist_read() -> list[dict] | None:
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            logger.error(f"Gist read failed: {r.status_code}")
            return None
        content = r.json()["files"].get(GIST_FILE, {}).get("content", "")
        if not content.strip():
            return []
        return list(csv.DictReader(io.StringIO(content)))
    except Exception as e:
        logger.error(f"Gist read error: {e}")
        return None


def _gist_append(row: dict) -> bool:
    existing = _gist_read()
    if existing is None:
        return False
    existing.append(row)
    return _gist_write(existing)


def _gist_write(rows: list[dict]) -> bool:
    try:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers(),
            json={"files": {GIST_FILE: {"content": buf.getvalue()}}},
            timeout=15,
        )
        if r.status_code in (200, 201):
            logger.info(f"Trade journal updated on Gist ({len(rows)} total records)")
            return True
        logger.error(f"Gist write failed: {r.status_code} — {r.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"Gist write error: {e}")
        return False


# ── Local file persistence ────────────────────────────────────────────────

def _local_append(row: dict) -> bool:
    try:
        write_header = not LOCAL_FILE.exists()
        with open(LOCAL_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        logger.info(f"Trade logged to {LOCAL_FILE}")
        return True
    except Exception as e:
        logger.error(f"Local log write error: {e}")
        return False


def _local_read() -> list[dict]:
    try:
        with open(LOCAL_FILE, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        logger.error(f"Local log read error: {e}")
        return []
