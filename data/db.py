"""
SQLite store for the long-running bot service (bot_service.py).

Tables:
  trades  — every auto-placed trade. Rows are written as OPEN the moment IQ
            Option accepts the order, then settled in place by order id, so a
            restart mid-trade never loses or double-counts a position.
  shadow  — the paper log (see data/shadow_log.py).
  status  — what the bot service publishes for the web app (web_app.py).
  push_subscriptions — browsers that enabled notifications in the PWA.

Path: BOT_DB_PATH env (default data/bot.db; /data/bot.db in Docker).
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("BOT_DB_PATH", "data/bot.db")

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT UNIQUE NOT NULL,
    broker         TEXT,                           -- deriv | iq   (each has its own account + balance)
    account        TEXT,
    placed_at      TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    pair           TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    direction      TEXT NOT NULL,
    session        TEXT,
    confidence     TEXT,
    strength       REAL,
    num_passed     INTEGER,
    checks_passed  TEXT,
    checks_failed  TEXT,
    price          REAL,
    adx            REAL,
    rsi            REAL,
    atr            REAL,
    macd_hist      REAL,
    bb_width       REAL,
    htf_trend      TEXT,
    pattern        TEXT,
    stake          REAL NOT NULL,
    payout         REAL,                           -- IQ turbo payout fraction at placement
    currency       TEXT DEFAULT 'USD',
    status         TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN | WIN | LOSS | TIE | UNKNOWN
    profit         REAL,
    balance_after  REAL,
    result_source  TEXT,                           -- socket | listinfo | history
    settled_at     TEXT
);
CREATE INDEX IF NOT EXISTS trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS shadow (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    pair          TEXT NOT NULL,
    direction     TEXT NOT NULL,
    num_passed    INTEGER NOT NULL,
    checks        TEXT NOT NULL,      -- 10 chars of 0/1, in strategy check order
    price         REAL,
    adx           REAL,
    plus_di       REAL,
    minus_di      REAL,
    rsi           REAL,
    atr           REAL,
    macd_hist     REAL,
    bb_width      REAL,
    htf_trend     TEXT,
    pattern       TEXT,
    session       TEXT,               -- pair's optimal session open now ('' = off-peak)
    blackout      TEXT,               -- active blackout window ('' = none)
    news_blocked  INTEGER,
    live          INTEGER DEFAULT 0,  -- live strategy emitted this signal
    traded        INTEGER DEFAULT 0,  -- an order was actually placed
    px_entry      REAL,
    px_5          REAL,
    px_15         REAL,
    px_30         REAL,
    res_5         TEXT,               -- WIN | LOSS | TIE | EXPIRED
    res_15        TEXT,
    res_30        TEXT
);
CREATE INDEX IF NOT EXISTS shadow_ts ON shadow(ts);
CREATE INDEX IF NOT EXISTS shadow_pending ON shadow(res_30);

-- Key/value status the bot service publishes for the web app (heartbeat, IQ state, balance...).
CREATE TABLE IF NOT EXISTS status (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint   TEXT PRIMARY KEY,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    user_agent TEXT,
    created_at TEXT NOT NULL
);
"""


@contextmanager
def connect():
    with _lock:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")
        # Migration: trades predating the broker column were all IQ Option.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
        for col in ("broker", "account"):
            if col not in cols:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} TEXT")
        c.execute("UPDATE trades SET broker='iq' WHERE broker IS NULL")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── Trades ────────────────────────────────────────────────────────────────

def insert_open_trade(row: dict) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with connect() as c:
        c.execute(f"INSERT OR IGNORE INTO trades ({cols}) VALUES ({marks})", list(row.values()))


def open_trades() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id")]


def settle_trade(order_id: str, status: str, profit: float | None,
                 balance_after: float | None, source: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE trades SET status=?, profit=?, balance_after=?, result_source=?, settled_at=? "
            "WHERE order_id=? AND status='OPEN'",
            (status, profit, balance_after, source, now_iso(), order_id),
        )


def pairs_traded_since(minutes: int) -> set[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with connect() as c:
        return {r["pair"] for r in c.execute("SELECT pair FROM trades WHERE placed_at > ?", (cutoff,))}


def recent_settled(limit: int, broker: str | None = None) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE status IN ('WIN','LOSS') AND (? IS NULL OR broker = ?) "
            "ORDER BY settled_at DESC LIMIT ?", (broker, broker, limit)
        )]


# ── Shadow ────────────────────────────────────────────────────────────────

def insert_shadow(row: dict) -> int:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with connect() as c:
        return c.execute(f"INSERT INTO shadow ({cols}) VALUES ({marks})", list(row.values())).lastrowid


def shadow_logged_since(pair: str, direction: str, since_iso: str) -> bool:
    with connect() as c:
        return c.execute(
            "SELECT 1 FROM shadow WHERE pair=? AND direction=? AND ts > ? LIMIT 1",
            (pair, direction, since_iso),
        ).fetchone() is not None


def shadow_update(row_id: int, fields: dict) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE shadow SET {sets} WHERE id=?", [*fields.values(), row_id])


def shadow_pending() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shadow WHERE res_5 IS NULL OR res_15 IS NULL OR res_30 IS NULL ORDER BY id"
        )]


def shadow_all() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM shadow ORDER BY id")]


# ── Status (bot service → web app) ────────────────────────────────────────

def set_status(values: dict) -> None:
    ts = now_iso()
    with connect() as c:
        c.executemany(
            "INSERT INTO status (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [(k, None if v is None else str(v), ts) for k, v in values.items()],
        )


def get_status() -> dict:
    with connect() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM status")}


# ── Push subscriptions ────────────────────────────────────────────────────

def add_subscription(endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth",
            (endpoint, p256dh, auth, user_agent[:200], now_iso()),
        )


def remove_subscription(endpoint: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))


def subscriptions() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM push_subscriptions")]


# ── Journal view (legacy CSV shape used by the dashboard stats + weekly report) ──

def journal_rows(broker: str | None = None) -> list[dict]:
    """Settled trades (optionally one broker's), oldest first, in the old Gist trade_log.csv shape."""
    from config import PAIR_DISPLAY
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE status != 'OPEN' AND (? IS NULL OR broker = ?) "
            "ORDER BY COALESCE(settled_at, placed_at), id", (broker, broker)
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "timestamp":     r["settled_at"] or r["placed_at"],
            "pair":          r["pair"],
            "pair_display":  PAIR_DISPLAY.get(r["pair"], r["pair"]),
            "direction":     r["direction"],
            "session":       r["session"] or "",
            "confidence":    r["confidence"] or "",
            "strength_pct":  f"{(r['strength'] or 0):.2%}",
            "adx":           f"{r['adx']:.2f}" if r["adx"] is not None else "",
            "rsi":           f"{r['rsi']:.2f}" if r["rsi"] is not None else "",
            "htf_trend":     r["htf_trend"] or "",
            "pattern":       r["pattern"] or "",
            "num_passed":    r["num_passed"] or "",
            "stake":         r["stake"],
            "currency":      r["currency"] or "USD",
            "outcome":       r["status"],
            "profit":        r["profit"] if r["profit"] is not None else 0.0,
            "balance_after": r["balance_after"] or 0.0,
            "contract_id":   r["order_id"],
            "source":        r["result_source"] or "",
        })
    return out
