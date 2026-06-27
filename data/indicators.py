import pandas as pd
import numpy as np
from config import (
    EMA_FAST, EMA_SLOW, EMA_TREND,
    ADX_PERIOD, RSI_PERIOD, ATR_PERIOD,
    BB_PERIOD, BB_STD,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
)


def add_ema(df: pd.DataFrame, period: int, col: str = "close") -> pd.DataFrame:
    df[f"ema_{period}"] = df[col].ewm(span=period, adjust=False).mean()
    return df


def add_rsi(df: pd.DataFrame, period: int = RSI_PERIOD, col: str = "close") -> pd.DataFrame:
    delta = df[col].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def add_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.DataFrame:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(com=period - 1, min_periods=period).mean()

    up_move   = high - high.shift()
    down_move = low.shift() - low
    plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm_s  = pd.Series(plus_dm,  index=df.index).ewm(com=period - 1, min_periods=period).mean()
    minus_dm_s = pd.Series(minus_dm, index=df.index).ewm(com=period - 1, min_periods=period).mean()

    plus_di  = 100 * plus_dm_s  / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm_s / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)

    df["adx"]      = dx.ewm(com=period - 1, min_periods=period).mean()
    df["plus_di"]  = plus_di
    df["minus_di"] = minus_di
    return df


def add_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.DataFrame:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=period - 1, min_periods=period).mean()
    return df


def add_bb(df: pd.DataFrame, period: int = BB_PERIOD, std: float = BB_STD, col: str = "close") -> pd.DataFrame:
    """Bollinger Bands + squeeze detection."""
    sma = df[col].rolling(period).mean()
    std_dev = df[col].rolling(period).std()
    df["bb_upper"]  = sma + std * std_dev
    df["bb_lower"]  = sma - std * std_dev
    df["bb_middle"] = sma
    # Width as a fraction of price — normalised so it's comparable across pairs
    df["bb_width"]  = (df["bb_upper"] - df["bb_lower"]) / sma.replace(0, np.nan)
    # Squeeze: True when BB width is at its lowest in the last 20 candles
    df["bb_squeeze"] = df["bb_width"] == df["bb_width"].rolling(period).min()
    return df


def add_macd(df: pd.DataFrame, fast: int = MACD_FAST, slow: int = MACD_SLOW,
             signal: int = MACD_SIGNAL, col: str = "close") -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    ema_fast = df[col].ewm(span=fast, adjust=False).mean()
    ema_slow = df[col].ewm(span=slow, adjust=False).mean()
    df["macd"]        = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]
    return df


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = add_ema(df, EMA_FAST)
    df = add_ema(df, EMA_SLOW)
    df = add_ema(df, EMA_TREND)
    df = add_rsi(df)
    df = add_adx(df)
    df = add_atr(df)
    df = add_bb(df)
    df = add_macd(df)
    df.dropna(inplace=True)
    return df
