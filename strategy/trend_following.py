"""
High-accuracy trend-following strategy — 10 confluence checks.

Minimum 75% pass rate (8/10) before a signal is emitted.

Checks:
  1.  HTF uptrend / downtrend  (H1 EMA alignment)
  2.  Price above / below EMA 50  (macro trend)
  3.  EMA 8 crossed EMA 21  (fresh momentum trigger)
  4.  EMA 8 aligned with EMA 21  (sustained momentum)
  5.  ADX above threshold  (trend is strong, not sideways)
  6.  DI directional bias  (+DI > -DI for BUY, vice versa)
  7.  RSI in neutral zone  (not overbought / oversold)
  8.  Candlestick pattern  (engulfing, hammer, 3-candle run)
  9.  MACD histogram positive and growing  (new — momentum confirmation)
  10. Bollinger Band breakout / squeeze resolution  (new — volatility expansion)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from config import (
    EMA_FAST, EMA_SLOW, EMA_TREND,
    HTF_EMA_FAST, HTF_EMA_SLOW,
    ADX_HARD_MIN, ADX_THRESHOLD,
    RSI_BULL_MIN, RSI_BULL_MAX, RSI_BEAR_MIN, RSI_BEAR_MAX,
    BB_SQUEEZE_THRESHOLD,
    MIN_SIGNAL_STRENGTH, SIGNAL_COOLDOWN_MINUTES,
    EXPIRY_MINUTES,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    direction: str                # "BUY" | "SELL" | "NONE"
    strength: float               # 0.0 – 1.0
    confidence_label: str         # "HIGH" | "MEDIUM" | "LOW" | "NO SIGNAL"
    checks_passed: list[str]
    checks_failed: list[str]
    price: float
    adx: float
    rsi: float
    ema_fast: float
    ema_slow: float
    atr: float
    htf_trend: str                # "UP" | "DOWN" | "NEUTRAL"
    candlestick_pattern: str
    macd_hist: float
    bb_width: float
    volume_ok: bool
    advice: str
    suggested_expiry: int = EXPIRY_MINUTES
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    upcoming_news: list = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return self.direction != "NONE"


# ── Candlestick pattern detection ────────────────────────────────────────

def detect_pattern(df: pd.DataFrame) -> str:
    if len(df) < 4:
        return ""
    c1, c2, c3 = df.iloc[-2], df.iloc[-3], df.iloc[-4]

    body1   = abs(c1["close"] - c1["open"])
    upper_w = c1["high"] - max(c1["open"], c1["close"])
    lower_w = min(c1["open"], c1["close"]) - c1["low"]
    is_bull1, is_bear1 = c1["close"] > c1["open"], c1["close"] < c1["open"]
    is_bull2, is_bear2 = c2["close"] > c2["open"], c2["close"] < c2["open"]

    if is_bull1 and is_bear2 and c1["open"] < c2["close"] and c1["close"] > c2["open"]:
        return "Bullish Engulfing"
    if is_bear1 and is_bull2 and c1["open"] > c2["close"] and c1["close"] < c2["open"]:
        return "Bearish Engulfing"
    if body1 > 0 and lower_w >= 2 * body1 and upper_w <= 0.3 * body1:
        return "Hammer"
    if body1 > 0 and upper_w >= 2 * body1 and lower_w <= 0.3 * body1:
        return "Shooting Star"

    is_bull3 = c3["close"] > c3["open"]
    is_bear3 = c3["close"] < c3["open"]
    if is_bull1 and is_bull2 and is_bull3:
        return "3 Bullish Candles"
    if is_bear1 and is_bear2 and is_bear3:
        return "3 Bearish Candles"

    return ""


# ── Higher-timeframe trend ────────────────────────────────────────────────

def get_htf_trend(htf_df: pd.DataFrame | None) -> str:
    if htf_df is None or len(htf_df) < HTF_EMA_SLOW + 5:
        return "NEUTRAL"
    ema_fast = htf_df["close"].ewm(span=HTF_EMA_FAST, adjust=False).mean().iloc[-1]
    ema_slow = htf_df["close"].ewm(span=HTF_EMA_SLOW, adjust=False).mean().iloc[-1]
    if ema_fast > ema_slow * 1.0001:
        return "UP"
    if ema_fast < ema_slow * 0.9999:
        return "DOWN"
    return "NEUTRAL"


# ── Strategy ─────────────────────────────────────────────────────────────

class TrendFollowingStrategy:
    _last_signal: dict[str, datetime] = {}

    def analyze(self, symbol: str, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> Signal:
        if len(df) < EMA_TREND + 10:
            return self._no_signal(symbol, df, "Not enough data")

        row  = df.iloc[-1]
        prev = df.iloc[-2]

        price     = row["close"]
        ema_fast  = row[f"ema_{EMA_FAST}"]
        ema_slow  = row[f"ema_{EMA_SLOW}"]
        ema_trend = row[f"ema_{EMA_TREND}"]
        prev_fast = prev[f"ema_{EMA_FAST}"]
        prev_slow = prev[f"ema_{EMA_SLOW}"]
        adx       = row["adx"]
        plus_di   = row["plus_di"]
        minus_di  = row["minus_di"]
        rsi       = row["rsi"]
        atr       = row["atr"]
        macd_hist      = row["macd_hist"]
        prev_macd_hist = prev["macd_hist"]
        bb_width       = row["bb_width"]
        bb_middle      = row["bb_middle"]
        bb_upper       = row["bb_upper"]
        bb_lower       = row["bb_lower"]

        # Was the market in a squeeze 1–5 candles ago?
        lookback = min(5, len(df) - 1)
        recent_squeeze = df["bb_squeeze"].iloc[-lookback:-1].any()

        vol_ma    = df["volume"].rolling(20).mean().iloc[-1]
        volume_ok = (vol_ma > 0) and (row["volume"] >= vol_ma * 1.1)

        pattern   = detect_pattern(df)
        htf_trend = get_htf_trend(htf_df)

        # ── Hard ADX gate — block ranging markets before scoring ──────
        if adx < ADX_HARD_MIN:
            logger.debug(f"SKIP {symbol}: ADX {adx:.1f} < {ADX_HARD_MIN} (ranging market — no trade)")
            return self._no_signal(symbol, df, f"ADX {adx:.1f} below minimum {ADX_HARD_MIN} — market is ranging", htf_trend=htf_trend)

        # ── 10 BUY checks ────────────────────────────────────────────
        buy = {
            "H1 trend aligned UP":                  htf_trend == "UP",
            "Price above EMA 50 (macro uptrend)":   price > ema_trend,
            "EMA 8 crossed above EMA 21 (trigger)": (ema_fast > ema_slow) and (prev_fast <= prev_slow),
            "EMA 8 above EMA 21 (momentum)":        ema_fast > ema_slow,
            "ADX above 28 (strong trend)":          adx >= ADX_THRESHOLD,
            "+DI above -DI (bullish pressure)":     plus_di > minus_di,
            "RSI in bullish zone (42–68)":          RSI_BULL_MIN <= rsi <= RSI_BULL_MAX,
            "Bullish candle pattern":               pattern in ("Bullish Engulfing", "Hammer", "3 Bullish Candles"),
            "MACD histogram positive and rising":   macd_hist > 0 and macd_hist > prev_macd_hist,
            "BB breakout above middle after squeeze": price > bb_middle and (recent_squeeze or bb_width > df["bb_width"].iloc[-10:-1].mean()),
        }

        # ── 10 SELL checks ───────────────────────────────────────────
        sell = {
            "H1 trend aligned DOWN":                  htf_trend == "DOWN",
            "Price below EMA 50 (macro downtrend)":   price < ema_trend,
            "EMA 8 crossed below EMA 21 (trigger)":   (ema_fast < ema_slow) and (prev_fast >= prev_slow),
            "EMA 8 below EMA 21 (momentum)":          ema_fast < ema_slow,
            "ADX above 28 (strong trend)":             adx >= ADX_THRESHOLD,
            "-DI above +DI (bearish pressure)":        minus_di > plus_di,
            "RSI in bearish zone (32–58)":             RSI_BEAR_MIN <= rsi <= RSI_BEAR_MAX,
            "Bearish candle pattern":                  pattern in ("Bearish Engulfing", "Shooting Star", "3 Bearish Candles"),
            "MACD histogram negative and falling":     macd_hist < 0 and macd_hist < prev_macd_hist,
            "BB breakout below middle after squeeze":  price < bb_middle and (recent_squeeze or bb_width > df["bb_width"].iloc[-10:-1].mean()),
        }

        buy_score  = sum(buy.values())  / len(buy)
        sell_score = sum(sell.values()) / len(sell)

        for direction, checks, score in [("BUY", buy, buy_score), ("SELL", sell, sell_score)]:
            if score < MIN_SIGNAL_STRENGTH:
                continue
            if direction == "SELL" and buy_score >= score:
                continue

            # H1 hard gate — never trade against the confirmed higher-timeframe trend.
            # NEUTRAL H1 is allowed (no clear bias). Only a confirmed opposite trend blocks.
            if direction == "BUY" and htf_trend == "DOWN":
                logger.info(f"BLOCK {symbol} BUY: H1 trend is DOWN — counter-trend trade blocked")
                continue
            if direction == "SELL" and htf_trend == "UP":
                logger.info(f"BLOCK {symbol} SELL: H1 trend is UP — counter-trend trade blocked")
                continue

            if self._on_cooldown(symbol):
                return self._no_signal(symbol, df, "Signal cooldown active", htf_trend=htf_trend)

            self._last_signal[symbol] = datetime.now(timezone.utc)
            passed = [k for k, v in checks.items() if v]
            failed = [k for k, v in checks.items() if not v]
            confidence = "HIGH" if score >= 0.90 else ("MEDIUM" if score >= 0.75 else "LOW")
            advice = _build_advice(direction, symbol, price, atr, score, passed, failed, pattern, htf_trend, rsi, adx, macd_hist, bb_width)

            sig = Signal(
                symbol=symbol, direction=direction, strength=score,
                confidence_label=confidence, checks_passed=passed, checks_failed=failed,
                price=price, adx=adx, rsi=rsi, ema_fast=ema_fast, ema_slow=ema_slow,
                atr=atr, htf_trend=htf_trend, candlestick_pattern=pattern,
                macd_hist=macd_hist, bb_width=bb_width, volume_ok=volume_ok, advice=advice,
            )
            logger.info(f"SIGNAL [{confidence}] {symbol} {direction} strength={score:.0%} adx={adx:.1f} rsi={rsi:.1f} macd_hist={macd_hist:.6f}")
            return sig

        # Pick the better-scoring direction and surface its actual check results
        # so NO SIGNAL log lines show which checks failed — not "none logged".
        if buy_score >= sell_score:
            best_checks, best_score, direction_label = buy, buy_score, "BUY"
        else:
            best_checks, best_score, direction_label = sell, sell_score, "SELL"
        passed = [k for k, v in best_checks.items() if v]
        failed = [k for k, v in best_checks.items() if not v]
        return self._no_signal(
            symbol, df,
            f"Filters not met (best={direction_label} {best_score:.0%})",
            checks_passed=passed, checks_failed=failed, htf_trend=htf_trend,
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _no_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        reason: str,
        checks_passed: list | None = None,
        checks_failed: list | None = None,
        htf_trend: str = "NEUTRAL",
    ) -> Signal:
        row = df.iloc[-1] if len(df) > 0 else pd.Series(dtype=float)
        g = lambda k, d=0.0: row.get(k, d) if hasattr(row, "get") else d
        return Signal(
            symbol=symbol, direction="NONE", strength=0.0,
            confidence_label="NO SIGNAL",
            checks_passed=checks_passed if checks_passed is not None else [],
            checks_failed=checks_failed if checks_failed is not None else [],
            price=g("close"), adx=g("adx"), rsi=g("rsi", 50.0),
            ema_fast=0.0, ema_slow=0.0, atr=g("atr"),
            htf_trend=htf_trend, candlestick_pattern="",
            macd_hist=g("macd_hist"), bb_width=g("bb_width"),
            volume_ok=False, advice=reason,
        )

    def _on_cooldown(self, symbol: str) -> bool:
        last = self._last_signal.get(symbol)
        return last is not None and (datetime.now(timezone.utc) - last).total_seconds() / 60 < SIGNAL_COOLDOWN_MINUTES


# ── Advice text ───────────────────────────────────────────────────────────

def _build_advice(direction, symbol, price, atr, score, passed, failed, pattern, htf_trend, rsi, adx, macd_hist, bb_width) -> str:
    from config import CURRENCY, PAIR_DISPLAY, EXPIRY_MINUTES, ACCOUNT_BALANCE, TRADE_AMOUNT_PCT, MAX_TRADE_AMOUNT, MIN_TRADE_AMOUNT
    name   = PAIR_DISPLAY.get(symbol, symbol)
    action = "CALL (↑ price will rise)" if direction == "BUY" else "PUT (↓ price will fall)"
    conf   = "HIGH" if score >= 0.90 else ("MEDIUM" if score >= 0.75 else "LOW")

    steps = [
        f"1. Open Pocket Option and select: {name}",
        f"2. Set direction to: {action}",
        f"3. Set expiry to: {EXPIRY_MINUTES} minute(s)",
        f"4. Suggested trade size: {CURRENCY}{max(MIN_TRADE_AMOUNT, min(MAX_TRADE_AMOUNT, ACCOUNT_BALANCE * TRADE_AMOUNT_PCT)):,.0f} (2% of your balance)",
        f"5. Wait for the current M5 candle to CLOSE before entering",
        f"6. Confirm price is still near {price:.5f} before placing the trade",
    ]
    if pattern:
        steps.append(f"7. Candle pattern '{pattern}' adds extra confirmation")

    why = []
    if htf_trend in ("UP", "DOWN"):
        why.append(f"H1 chart is trending {'UP' if direction == 'BUY' else 'DOWN'} — big picture supports this trade.")
    if adx >= 30:
        why.append(f"ADX = {adx:.1f} (very strong trend) — price is moving with conviction.")
    elif adx >= 28:
        why.append(f"ADX = {adx:.1f} (confirmed trend) — enough momentum to hold through expiry.")
    if macd_hist > 0 and direction == "BUY":
        why.append(f"MACD histogram is positive and rising — buyers are accelerating.")
    if macd_hist < 0 and direction == "SELL":
        why.append(f"MACD histogram is negative and falling — sellers are accelerating.")
    if "BB breakout" in " ".join(passed):
        why.append(f"Bollinger Bands expanding after a squeeze — a directional move is underway.")

    cautions = []
    for f_check in failed:
        if "ADX" in f_check:
            cautions.append("ADX is below 28 — trend strength is weaker than ideal. Consider skipping.")
        if "H1 trend" in f_check:
            cautions.append("H1 trend not fully aligned — trade with reduced size.")
        if "MACD" in f_check:
            cautions.append("MACD not confirming — momentum may not be strong enough.")

    lines = [
        f"CONFIDENCE: {conf} ({score:.0%} of filters passed)\n",
        "HOW TO TRADE:", *steps, "",
        "WHY THIS SIGNAL:", *[f"• {w}" for w in why], "",
    ]
    if cautions:
        lines += ["CAUTIONS:", *[f"• {c}" for c in cautions], ""]
    lines += [
        "RISK RULES:",
        f"• Never risk more than 2% of your balance ({CURRENCY}{max(MIN_TRADE_AMOUNT, min(MAX_TRADE_AMOUNT, ACCOUNT_BALANCE * TRADE_AMOUNT_PCT)):,.0f}) on one trade.",
        "• Stop trading for the day after 3 consecutive losses.",
        "• This signal is high-probability but NOT guaranteed.",
    ]
    return "\n".join(lines)
