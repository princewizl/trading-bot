"""
Real-time OHLCV data from Deriv's public WebSocket price feed.

No authentication required — Deriv's tick/candle history is publicly accessible.
This replaces OANDA as the primary real-time data source.

Same prices the bot trades on → no slippage from data/execution price mismatch.
"""

import json
import logging
import time

import pandas as pd
import websocket

import config

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws.binaryws.com/websockets/v3?app_id={app_id}"

_GRANULARITY = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "4h":  14400,
    "1d":  86400,
}


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

    try:
        url = _WS_URL.format(app_id=config.DERIV_APP_ID)
        ws  = websocket.create_connection(url, timeout=20)
        try:
            ws.send(json.dumps({
                "ticks_history": deriv_symbol,
                "end":           "latest",
                "count":         count,
                "style":         "candles",
                "granularity":   granularity,
            }))

            deadline = time.time() + 15
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                ws.settimeout(remaining)
                resp = json.loads(ws.recv())

                if "error" in resp:
                    logger.error(f"Deriv feed error for {symbol}: {resp['error']['message']}")
                    return None

                if resp.get("msg_type") == "candles":
                    return _to_dataframe(resp["candles"])

        finally:
            ws.close()

    except Exception as e:
        logger.error(f"Deriv feed fetch failed for {symbol}: {e}")

    return None


def is_available() -> bool:
    """Quick check — True if Deriv WebSocket is reachable."""
    try:
        url = _WS_URL.format(app_id=config.DERIV_APP_ID)
        ws  = websocket.create_connection(url, timeout=10)
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
