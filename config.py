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
ADX_HARD_MIN  = 22         # Hard floor — signal blocked entirely below this, regardless of other checks
ADX_THRESHOLD = 28         # Soft check inside the 10-confluence scoring (one of 10 checks)

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

# ─── Data Source ─────────────────────────────────────────────────────────────
# Primary: Deriv WebSocket feed (real-time, no account needed — uses DERIV_APP_ID)
# Fallback: yfinance (15-30 min delay)
# OANDA removed: not available in Nigeria

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

# ─── Twelve Data (real-time forex feed, fallback when Deriv WebSocket fails) ──
# Sign up free: https://twelvedata.com/ → Dashboard → API Keys
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

# ─── IQ Option Integration ───────────────────────────────────────────────────
# Sign up free: iqoption.com — demo account starts with $10,000 virtual
# Add to .env:  IQ_EMAIL, IQ_PASSWORD, IQ_DEMO=true
IQ_EMAIL    = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
IQ_DEMO     = os.getenv("IQ_DEMO", "true").lower() == "true"   # true = practice/$10k virtual

# yfinance symbol → IQ Option ticker (binary options only)
# USDJPY: not available as binary options on IQ Option — signal emails only
# EURGBP: removed — 47.8% win rate (below 58.8% break-even at 70% payout), signal emails only
IQ_SYMBOLS = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "AUDUSD=X": "AUDUSD",
    "USDCAD=X": "USDCAD",
    "GBPJPY=X": "GBPJPY",
}

# Deriv price feed (still used for real-time OHLCV data — no account needed)
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DERIV_SYMBOLS = {
    "EURUSD=X": "frxEURUSD",
    "GBPUSD=X": "frxGBPUSD",
    "USDJPY=X": "frxUSDJPY",
    "AUDUSD=X": "frxAUDUSD",
    "USDCAD=X": "frxUSDCAD",
    "EURGBP=X": "frxEURGBP",
    "GBPJPY=X": "frxGBPJPY",
}

# ─── Email Settings ─────────────────────────────────────────────────────────
EMAIL_SENDER    = os.getenv("EMAIL_SENDER", "")       # your Gmail address
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")     # Gmail App Password (not your login password)
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "")    # where signals are sent
SMTP_HOST       = "smtp.hmailplus.com"
SMTP_PORT       = 587

# ─── Currency ───────────────────────────────────────────────────────────────
CURRENCY = "₦"   # Nigerian Naira

# ─── Risk / Session Limits ───────────────────────────────────────────────────
ACCOUNT_BALANCE      = float(os.getenv("ACCOUNT_BALANCE", "50000"))
TRADE_AMOUNT_PCT     = 0.02    # Stake = 2% of live Deriv balance
MAX_TRADE_AMOUNT     = 10_000.0  # No cap — 2% rule governs; IQ Option's own limit is the real ceiling
MIN_TRADE_AMOUNT     = 1.0    # Deriv minimum stake (most forex pairs)
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_SIGNALS    = 50      # Cap emails per day

# ─── Martingale (keep disabled — extremely high risk) ───────────────────────
MARTINGALE_ENABLED    = False
MARTINGALE_MULTIPLIER = 2.0
MARTINGALE_MAX_STEPS  = 3

# ─── Legacy Pocket Option settings (unused in email-only mode) ──────────────
TRADING_MODE        = "signal"
POCKET_OPTION_SSID  = os.getenv("PO_SSID", "")
PO_ASSET_MAP        = {
    "EURUSD=X": "#EURUSD_otc", "GBPUSD=X": "#GBPUSD_otc",
    "USDJPY=X": "#USDJPY_otc", "AUDUSD=X": "#AUDUSD_otc",
    "USDCAD=X": "#USDCAD_otc", "EURGBP=X": "#EURGBP_otc",
    "GBPJPY=X": "#GBPJPY_otc",
}
TRADE_EXPIRY_SECONDS = EXPIRY_MINUTES * 60

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_FILE        = "logs/bot.log"
TRADE_LOG_FILE  = "logs/trades.csv"
