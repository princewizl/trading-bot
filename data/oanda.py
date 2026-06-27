"""
OANDA v20 REST API data client.

Provides real-time Forex OHLCV data with no delay — unlike yfinance which
can lag 15–30 minutes on its free feed.

Requirements:
  1. Free OANDA practice account: https://www.oanda.com/register/#/sign-up/demo
  2. API token: My Account → Manage API Access → Generate
  3. Add to .env:
       OANDA_API_KEY=your_token_here
       OANDA_ACCOUNT_ID=your_account_id_here   (found in account dashboard)

If OANDA_API_KEY is not set, all functions return None and the caller
falls back to yfinance automatically.
"""

import logging
from datetime import timezone

import pandas as pd
import requests

from config import OANDA_API_KEY, OANDA_ENV, OANDA_INSTRUMENTS

logger = logging.getLogger(__name__)

_BASE = {
    "practice": "https://api-fxpractice.oanda.com",
    "live":     "https://api-fxtrade.oanda.com",
}

# yfinance interval → OANDA granularity
_GRANULARITY = {
    "1m":  "M1",
    "5m":  "M5",
    "15m": "M15",
    "1h":  "H1",
    "4h":  "H4",
    "1d":  "D",
}

# yfinance period string → approximate candle count
_PERIOD_CANDLES = {
    "1d":  288,    # 1d of M5 = 288 candles
    "2d":  576,
    "5d":  500,    # capped at OANDA max 500
    "7d":  500,
    "30d": 500,
    "60d": 500,
    "90d": 500,
}


def is_configured() -> bool:
    return bool(OANDA_API_KEY)


def fetch_ohlcv(symbol: str, interval: str = "5m", period: str = "5d") -> pd.DataFrame | None:
    """
    Fetch OHLCV candles from OANDA.
    Returns a DataFrame with columns [open, high, low, close, volume] indexed by UTC datetime.
    Returns None if OANDA is not configured or the request fails.
    """
    if not is_configured():
        return None

    instrument  = OANDA_INSTRUMENTS.get(symbol)
    granularity = _GRANULARITY.get(interval)
    count       = _PERIOD_CANDLES.get(period, 200)

    if not instrument or not granularity:
        logger.warning(f"OANDA: unknown symbol/interval {symbol}/{interval}")
        return None

    url = f"{_BASE[OANDA_ENV]}/v3/instruments/{instrument}/candles"
    params = {
        "count":       count,
        "granularity": granularity,
        "price":       "M",          # midpoint (bid+ask)/2
    }
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type":  "application/json",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        logger.error(f"OANDA HTTP error for {symbol}: {e} — {resp.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"OANDA request failed for {symbol}: {e}")
        return None

    candles = data.get("candles", [])
    if not candles:
        logger.warning(f"OANDA returned no candles for {symbol}")
        return None

    rows = []
    for c in candles:
        if not c.get("complete", True):
            continue   # skip the currently forming candle
        mid = c.get("mid", {})
        try:
            rows.append({
                "datetime": pd.Timestamp(c["time"]).tz_convert(timezone.utc),
                "open":     float(mid["o"]),
                "high":     float(mid["h"]),
                "low":      float(mid["l"]),
                "close":    float(mid["c"]),
                "volume":   int(c.get("volume", 0)),
            })
        except (KeyError, ValueError):
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows).set_index("datetime")
    df.index.name = None
    logger.debug(f"OANDA: {len(df)} candles for {symbol} [{granularity}]")
    return df
