import logging

import pandas as pd
import yfinance as yf

from config import SIGNAL_INTERVAL as CANDLE_INTERVAL, SIGNAL_PERIOD as CANDLE_LOOKBACK
from data import oanda

logger = logging.getLogger(__name__)

_oanda_warned = False   # log OANDA fallback only once


def fetch_ohlcv(symbol: str, interval: str = CANDLE_INTERVAL, period: str = CANDLE_LOOKBACK) -> pd.DataFrame | None:
    """
    Fetch OHLCV candles. Uses OANDA (real-time, no delay) when OANDA_API_KEY
    is configured; falls back to yfinance otherwise.
    """
    # ── Try OANDA first ───────────────────────────────────────────────────
    if oanda.is_configured():
        df = oanda.fetch_ohlcv(symbol, interval=interval, period=period)
        if df is not None and not df.empty:
            return df
        logger.warning(f"OANDA returned no data for {symbol} — falling back to yfinance")

    # ── yfinance fallback ─────────────────────────────────────────────────
    global _oanda_warned
    if not oanda.is_configured() and not _oanda_warned:
        logger.info("OANDA not configured — using yfinance (may have 15-30 min delay). "
                    "Add OANDA_API_KEY to .env for real-time data.")
        _oanda_warned = True

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
        "open":   round(row["open"],  5),
        "high":   round(row["high"],  5),
        "low":    round(row["low"],   5),
        "close":  round(row["close"], 5),
        "volume": int(row["volume"]),
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
        df.columns = ["open", "high", "low", "close", "volume"]
        df.index = pd.to_datetime(df.index, utc=True)
        df.dropna(inplace=True)
        logger.debug(f"yfinance: {len(df)} candles for {symbol} [{interval}]")
        return df
    except Exception as e:
        logger.error(f"yfinance error for {symbol}: {e}")
        return None
