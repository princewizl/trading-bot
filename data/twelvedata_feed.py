"""
Twelve Data real-time forex OHLCV feed.

Used as fallback when the Deriv WebSocket feed is unavailable.
Much better than yfinance — data is real-time (0–1 min delay on forex).

Free tier: 800 API credits/day, 8 requests/min — plenty for occasional fallback use.

Setup:
  1. Sign up free at https://twelvedata.com/
  2. Dashboard → API Keys → copy your key
  3. Add to .env:  TWELVEDATA_API_KEY=your_key_here
  4. Add to GitHub Secrets: TWELVEDATA_API_KEY
"""

import logging
from datetime import datetime, timezone

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"

# yfinance symbol → Twelve Data symbol
_SYMBOL_MAP = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "EURGBP=X": "EUR/GBP",
    "GBPJPY=X": "GBP/JPY",
}

# Our interval format → Twelve Data format
_INTERVAL_MAP = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1day",
}

# Period → number of candles to request
_PERIOD_CANDLES = {
    "1d":  300,
    "5d":  500,
    "7d":  700,
    "30d": 800,
}


def fetch_ohlcv(symbol: str, interval: str = "5m", period: str = "5d", api_key: str = "") -> pd.DataFrame | None:
    """
    Fetch OHLCV candles from Twelve Data.
    Returns normalised DataFrame (lowercase columns, UTC index) or None on failure.
    """
    if not api_key:
        return None

    td_symbol   = _SYMBOL_MAP.get(symbol)
    td_interval = _INTERVAL_MAP.get(interval)
    outputsize  = _PERIOD_CANDLES.get(period, 400)

    if not td_symbol or not td_interval:
        logger.debug(f"Twelve Data: no mapping for {symbol}/{interval}")
        return None

    try:
        resp = requests.get(
            f"{BASE_URL}/time_series",
            params={
                "symbol":     td_symbol,
                "interval":   td_interval,
                "outputsize": outputsize,
                "apikey":     api_key,
                "timezone":   "UTC",
                "format":     "JSON",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "error":
            logger.warning(f"Twelve Data error for {symbol}: {data.get('message', '?')}")
            return None

        values = data.get("values", [])
        if not values:
            logger.warning(f"Twelve Data returned no candles for {symbol}")
            return None

        df = _to_dataframe(values)
        if df is not None:
            logger.info(f"Twelve Data: {len(df)} candles for {symbol} [{interval}]")
        return df

    except Exception as e:
        logger.warning(f"Twelve Data fetch failed for {symbol}: {e}")
        return None


def _to_dataframe(values: list) -> pd.DataFrame | None:
    rows = []
    for v in reversed(values):   # API returns newest-first; we want oldest-first
        try:
            rows.append({
                "datetime": datetime.fromisoformat(v["datetime"]).replace(tzinfo=timezone.utc),
                "open":     float(v["open"]),
                "high":     float(v["high"]),
                "low":      float(v["low"]),
                "close":    float(v["close"]),
                "volume":   float(v.get("volume", 0)),
            })
        except Exception:
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows).set_index("datetime")
    df.index.name = None
    return df
