import os
from dotenv import load_dotenv

load_dotenv()

# ─── Trading Pairs ─────────────────────────────────────────────────────────
TRADING_PAIRS = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "EURGBP=X",
    "GBPJPY=X",
]

# Human-readable names for emails
PAIR_DISPLAY = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "EURGBP=X": "EUR/GBP",
    "GBPJPY=X": "GBP/JPY",
}

# ─── Timeframes ─────────────────────────────────────────────────────────────
SIGNAL_INTERVAL  = "5m"    # Signal generation timeframe (M5)
TREND_INTERVAL   = "1h"    # Higher-timeframe trend filter (H1)
SIGNAL_PERIOD    = "5d"    # History to fetch for M5
TREND_PERIOD     = "30d"   # History to fetch for H1

# Recommended binary option expiry for each signal timeframe
EXPIRY_MINUTES   = 5       # Recommended trade expiry in minutes

# ─── Accuracy-Tuned Strategy Parameters ─────────────────────────────────────
EMA_FAST   = 8
EMA_SLOW   = 21
EMA_TREND  = 50            # Used on M5

# Higher-timeframe EMAs (applied to H1 data)
HTF_EMA_FAST = 10
HTF_EMA_SLOW = 30

ADX_PERIOD    = 14
ADX_THRESHOLD = 28         # Raised from 25 — requires confirmed trend strength

RSI_PERIOD       = 14
RSI_OVERBOUGHT   = 72
RSI_OVERSOLD     = 28
RSI_BULL_MIN     = 42      # RSI must be in this zone for a BUY
RSI_BULL_MAX     = 68
RSI_BEAR_MIN     = 32      # RSI must be in this zone for a SELL
RSI_BEAR_MAX     = 58

ATR_PERIOD = 14

VOLUME_MA_PERIOD = 20      # Volume must be above this moving average
VOLUME_MIN_RATIO = 1.1     # Volume must be 10% above MA to confirm

# ─── Bollinger Bands ────────────────────────────────────────────────────────
BB_PERIOD           = 20
BB_STD              = 2.0
BB_SQUEEZE_THRESHOLD = 0.0025  # BB width/price < 0.25% = squeeze on forex

# ─── MACD ───────────────────────────────────────────────────────────────────
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# ─── OANDA Real-time Data ───────────────────────────────────────────────────
# Free practice account: https://www.oanda.com/register/#/sign-up/demo
# Get API token: My Account → Manage API Access
OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV        = "practice"   # "practice" or "live"

# Map yfinance symbols → OANDA instrument names
OANDA_INSTRUMENTS = {
    "EURUSD=X": "EUR_USD",
    "GBPUSD=X": "GBP_USD",
    "USDJPY=X": "USD_JPY",
    "AUDUSD=X": "AUD_USD",
    "USDCAD=X": "USD_CAD",
    "EURGBP=X": "EUR_GBP",
    "GBPJPY=X": "GBP_JPY",
}

# ─── Correlated Pair Filter ─────────────────────────────────────────────────
# When two pairs in the same group fire the same direction in one scan,
# only the highest-strength signal is sent — the rest are redundant.
CORRELATION_GROUPS = [
    ["EURUSD=X", "GBPUSD=X", "AUDUSD=X"],   # all move against USD together
    ["EURUSD=X", "EURGBP=X"],                # EUR base pairs
    ["GBPUSD=X", "EURGBP=X", "GBPJPY=X"],   # GBP base pairs
]

# Minimum fraction of checks that must pass to emit a signal
MIN_SIGNAL_STRENGTH = 0.75  # 75% = at least 8 of 10 checks must pass

# Minimum candles between two signals on the same pair
SIGNAL_COOLDOWN_MINUTES = 15

# ─── Email Settings ─────────────────────────────────────────────────────────
EMAIL_SENDER    = os.getenv("EMAIL_SENDER", "")       # your Gmail address
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")     # Gmail App Password (not your login password)
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "")    # where signals are sent
SMTP_HOST       = "smtp.hmailplus.com"
SMTP_PORT       = 587

# ─── Risk / Session Limits ───────────────────────────────────────────────────
ACCOUNT_BALANCE      = float(os.getenv("ACCOUNT_BALANCE", "1000"))
TRADE_AMOUNT_PCT     = 0.02    # Suggested trade size: 2% of balance
MAX_TRADE_AMOUNT     = 50.0
MIN_TRADE_AMOUNT     = 1.0
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_LOSS_PCT   = 0.06
MAX_DAILY_SIGNALS    = 15      # Cap emails per day

# ─── Martingale (keep disabled — extremely high risk) ───────────────────────
MARTINGALE_ENABLED    = False
MARTINGALE_MULTIPLIER = 2.0
MARTINGALE_MAX_STEPS  = 3

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_FILE        = "logs/bot.log"
TRADE_LOG_FILE  = "logs/trades.csv"
