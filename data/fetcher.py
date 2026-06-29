"""
OHLCV data fetcher.

Priority order:
  1. Deriv WebSocket feed  — real-time, same prices the bot trades on, no account needed
  2. yfinance              — free fallback, 15-30 min delay on forex

OANDA removed: not available in Nigeria.
"""

import logging

import pandas as pd
import yfinance as yf

from config import SIGNAL_INTERVAL as CANDLE_INTERVAL, SIGNAL_PERIOD as CANDLE_LOOKBACK
from data import deriv_feed

logger = logging.getLogger(__name__)

_yf_warned = False


def fetch_ohlcv(symbol: str, interval: str = CANDLE_INTERVAL, period: str = CANDLE_LOOKBACK) -> pd.DataFrame | None:
    """
    Fetch OHLCV candles for a symbol.
    Tries Deriv price feed first (real-time), falls back to yfinance.
    """
    # ── 1. Deriv real-time feed ───────────────────────────────────────────
    df = deriv_feed.fetch_ohlcv(symbol, interval=interval, period=period)
    if df is not None and not df.empty:
        logger.debug(f"Deriv feed: {len(df)} candles for {symbol} [{interval}]")
        return _normalize(df)

    logger.warning(f"Deriv feed returned no data for {symbol} — falling back to yfinance")

    # ── 2. yfinance fallback ──────────────────────────────────────────────
    global _yf_warned
    if not _yf_warned:
        logger.info("Using yfinance as fallback (15-30 min delay on forex).")
        _yf_warned = True

    return _yfinance_fetch(symbol, interval, period)


def fetch_multiple(symbols: list[str], interval: str = CANDLE_INTERVAL, period: str = CANDLE_LOOKBACK) -> dict[str, pd.DataFrame]:
    return {s: df for s in symbols if (df := fetch_ohlcv(s, interval, period)) is not None}


def get_latest_candle(symbol: str) -> dict | None:
    df = fetch_ohlcv(symbol, interval="5m", period="1d")
    if df is None or len(df) < 2:
        return None
    row = df.iloc[-2]
    return {
        "symbol": symbol,
        "time":   df.index[-2],
        "open":   round(float(row["Open"]), 5),
        "high":   round(float(row["High"]), 5),
        "low":    round(float(row["Low"]),  5),
        "close":  round(float(row["Close"]), 5),
        "volume": 0,
    }


# ── yfinance internals ────────────────────────────────────────────────────

def _yfinance_fetch(symbol: str, interval: str, period: str) -> pd.DataFrame | None:
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            logger.warning(f"yfinance: no data for {symbol}")
            return None
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df.dropna(inplace=True)
        logger.debug(f"yfinance: {len(df)} candles for {symbol} [{interval}]")
        return _normalize(df)
    except Exception as e:
        logger.error(f"yfinance error for {symbol}: {e}")
        return None


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure all column names are lowercase so indicators.py finds them consistently."""
    df.columns = [c.lower() for c in df.columns]
    return df
