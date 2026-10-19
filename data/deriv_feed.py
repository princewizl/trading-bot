"""
Real-time OHLCV data from Deriv's public WebSocket price feed.

No authentication required — Deriv's tick/candle history is publicly accessible.
This replaces OANDA as the primary real-time data source.

Same prices the bot trades on → no slippage from data/execution price mismatch.
"""

import itertools
import json
import logging
import threading
import time

import pandas as pd
import websocket

import config

logger = logging.getLogger(__name__)

# Public market-data socket of Deriv's current API (no auth needed for
# ticks_history). The legacy hosts (ws.binaryws.com / ws.derivws.com) started
# rejecting every handshake with HTTP 520, which silently pushed all fetches onto
# Twelve Data / delayed yfinance. Legacy is kept only as a last attempt.
_WS_URLS = [
    "wss://api.derivws.com/trading/v1/options/ws/public?app_id={app_id}",
    "wss://ws.derivws.com/websockets/v3?app_id={app_id}",
]

_GRANULARITY = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "4h":  14400,
    "1d":  86400,
}

# One socket reused across calls (and across scans in the long-running service).
# Opening a fresh connection per fetch — 18+ per scan — gets throttled.
_lock   = threading.Lock()
_ws     = None
_req_id = itertools.count(1)


def fetch_ohlcv(symbol: str, interval: str = "5m", period: str = "5d") -> pd.DataFrame | None:
    """
    Fetch OHLCV candles from Deriv WebSocket.
    symbol:   yfinance-style symbol e.g. "EURUSD=X"
    interval: "5m", "1h", etc.
    period:   "5d", "30d", etc.

    Returns a DataFrame with columns Open/High/Low/Close/Volume indexed by UTC datetime,
    or None on failure.
    """
    deriv_symbol = config.DERIV_SYMBOLS.get(symbol)
    if not deriv_symbol:
        logger.debug(f"No Deriv symbol mapping for {symbol}")
        return None

    granularity = _GRANULARITY.get(interval)
    if not granularity:
        logger.warning(f"Unsupported interval '{interval}' for Deriv feed")
        return None

    count = _period_to_count(period, interval)
    resp = _request({
        "ticks_history": deriv_symbol,
        "end":           "latest",
        "count":         count,
        "style":         "candles",
        "granularity":   granularity,
    })
    if resp is None:
        logger.error(f"Deriv feed unreachable for {symbol}")
        return None
    if "error" in resp:
        logger.error(f"Deriv feed error for {symbol}: {resp['error']['message']}")
        return None
    return _to_dataframe(resp.get("candles", []))


def fetch_batch(items: list[tuple[str, str, str]], timeout: float = 20) -> dict[tuple, pd.DataFrame]:
    """
    Fetch many (symbol, interval, period) series in one round trip: every
    request goes out on the shared socket before any reply is read, so total
    time is ~one network latency instead of one per series. Missing / failed
    items are simply absent from the result (callers fall back per item).
    """
    global _ws
    out: dict[tuple, pd.DataFrame] = {}
    with _lock:
        # Deriv drops idle sockets between scans, so the first send after a pause
        # often hits a dead connection: reconnect once and resend what's missing.
        for attempt in range(2):
            todo = [i for i in items if i not in out]
            if not todo:
                break
            if _ws is None:
                _ws = _open()
                if _ws is None:
                    break
            pending: dict[int, tuple] = {}
            try:
                for item in todo:
                    symbol, interval, period = item
                    deriv_symbol, granularity = config.DERIV_SYMBOLS.get(symbol), _GRANULARITY.get(interval)
                    if not deriv_symbol or not granularity:
                        continue
                    req_id = next(_req_id)
                    pending[req_id] = item
                    _ws.send(json.dumps({
                        "ticks_history": deriv_symbol, "end": "latest",
                        "count": _period_to_count(period, interval),
                        "style": "candles", "granularity": granularity, "req_id": req_id,
                    }))
                deadline = time.time() + timeout
                while pending and time.time() < deadline:
                    _ws.settimeout(max(0.1, deadline - time.time()))
                    resp = json.loads(_ws.recv())
                    item = pending.pop(resp.get("req_id"), None)
                    if item is None:
                        continue
                    if "error" in resp:
                        logger.error(f"Deriv feed error for {item[0]} [{item[1]}]: {resp['error']['message']}")
                        continue
                    df = _to_dataframe(resp.get("candles", []))
                    if df is not None:
                        out[item] = df
                break
            except Exception as e:
                logger.debug(f"Deriv batch attempt {attempt + 1} interrupted ({e}) — reconnecting")
                _close()
        missing = len(items) - len(out)
        if missing:
            logger.warning(f"Deriv batch: {missing} item(s) missing — falling back per item")
    return out


def _request(payload: dict, timeout: float = 15) -> dict | None:
    """Send one request on the shared socket and return its reply (matched by req_id)."""
    global _ws
    with _lock:
        for _ in range(2):   # second pass = fresh connection after a dropped one
            if _ws is None:
                _ws = _open()
                if _ws is None:
                    return None
            req_id = next(_req_id)
            try:
                _ws.send(json.dumps({**payload, "req_id": req_id}))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    _ws.settimeout(max(0.1, deadline - time.time()))
                    resp = json.loads(_ws.recv())
                    if resp.get("req_id") == req_id:
                        return resp
                raise TimeoutError("no reply")
            except Exception as e:
                logger.debug(f"Deriv socket error ({e}) — reconnecting")
                _close()
    return None


def _open():
    for url_tpl in _WS_URLS:
        try:
            return websocket.create_connection(url_tpl.format(app_id=config.DERIV_APP_ID), timeout=20)
        except Exception as e:
            logger.debug(f"Deriv connect failed ({url_tpl.split('?')[0]}): {e}")
    return None


def _close():
    global _ws
    try:
        if _ws is not None:
            _ws.close()
    except Exception:
        pass
    _ws = None


def is_available() -> bool:
    """Quick check — True if Deriv WebSocket is reachable."""
    try:
        ws = websocket.create_connection(_WS_URLS[0].format(app_id=config.DERIV_APP_ID), timeout=10)
        ws.close()
        return True
    except Exception:
        return False


# ── Internal helpers ──────────────────────────────────────────────────────

def _period_to_count(period: str, interval: str) -> int:
    """Convert a period string (e.g. "5d") to a candle count."""
    interval_minutes = _GRANULARITY.get(interval, 300) // 60

    multipliers = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    try:
        unit  = period[-1].lower()
        value = int(period[:-1])
        total_minutes = value * multipliers.get(unit, 1440)
        count = (total_minutes // interval_minutes) + 10   # +10 buffer for indicator warmup
        return min(count, 4990)    # Deriv max is 5000 candles
    except Exception:
        return 500   # safe default


def _to_dataframe(candles: list) -> pd.DataFrame | None:
    if not candles:
        return None

    df = pd.DataFrame(candles)
    df["datetime"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    df = df.rename(columns={
        "open":  "Open",
        "high":  "High",
        "low":   "Low",
        "close": "Close",
    })
    df["Volume"] = 0   # Deriv forex has no volume data
    df = df.set_index("datetime")[["Open", "High", "Low", "Close", "Volume"]]
    return df.astype(float)
