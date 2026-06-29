"""
IQ Option trading client.

Uses the iqoptionapi community library to place binary options (Higher/Lower)
and retrieve results automatically.

Setup:
  1. Create a free IQ Option account at iqoption.com
  2. Add to .env:
       IQ_EMAIL=your_email@example.com
       IQ_PASSWORD=your_password
       IQ_DEMO=true     ← start on demo ($10,000 virtual)

BUY signal  → "call" (Higher) — win if price is higher at expiry
SELL signal → "put"  (Lower)  — win if price is lower at expiry

IQ Option automatically provides OTC (24/7) variants of forex pairs.
"""

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# yfinance symbol → IQ Option ticker (tries live first, OTC fallback)
IQ_SYMBOLS = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "USDCAD=X": "USDCAD",
    "EURGBP=X": "EURGBP",
    "GBPJPY=X": "GBPJPY",
}


class IQClient:
    def __init__(self, email: str, password: str, demo: bool = True):
        self.email    = email
        self.password = password
        self.demo     = demo
        self.iq       = None
        self.balance  = 0.0
        self.currency = "USD"

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            from iqoptionapi.stable_api import IQ_Option
            self.iq = IQ_Option(self.email, self.password)
            check, reason = self.iq.connect()

            if not check:
                logger.error(f"IQ Option login failed: {reason}")
                return False

            account = "PRACTICE" if self.demo else "REAL"
            self.iq.change_balance(account)
            self.balance = self.iq.get_balance()

            logger.info(
                f"IQ Option connected ({'DEMO/PRACTICE' if self.demo else 'LIVE/REAL'}) | "
                f"Balance: USD {self.balance:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"IQ Option connect error: {e}")
            return False

    def disconnect(self):
        try:
            if self.iq:
                self.iq.close()
        except Exception:
            pass

    # ── Balance ───────────────────────────────────────────────────────────

    def refresh_balance(self) -> float:
        try:
            self.balance = self.iq.get_balance()
        except Exception:
            pass
        return self.balance

    # ── Trade placement ───────────────────────────────────────────────────

    def place_trade(self, symbol: str, direction: str, amount: float, duration_minutes: int = 5) -> dict | None:
        """
        Place a Higher/Lower binary option.
        symbol:    IQ Option ticker, e.g. "EURUSD"
        direction: "BUY" → call (Higher) | "SELL" → put (Lower)

        Returns a trade record dict, or None on failure.
        Automatically falls back to OTC variant if the regular market is closed.
        """
        action = "call" if direction == "BUY" else "put"
        amount = round(amount, 2)

        # Try regular market first, then OTC (24/7 synthetic)
        for ticker in [symbol, f"{symbol}-OTC"]:
            status, trade_id = self._buy(ticker, action, amount, duration_minutes)
            if status:
                expiry_epoch = int(time.time()) + (duration_minutes * 60)
                trade = {
                    "trade_id":    trade_id,
                    "symbol":      ticker,
                    "yf_symbol":   symbol,
                    "direction":   direction,
                    "action":      action,
                    "stake":       amount,
                    "expiry_epoch": expiry_epoch,
                    "expiry_dt":   datetime.fromtimestamp(expiry_epoch, tz=timezone.utc).isoformat(),
                    "currency":    "USD",
                    "placed_at":   datetime.now(timezone.utc).isoformat(),
                    "otc":         ticker.endswith("-OTC"),
                }
                logger.info(
                    f"TRADE PLACED: {ticker} {direction} "
                    f"USD {amount:.2f} | id={trade_id} | "
                    f"expires={trade['expiry_dt']}"
                    + (" [OTC]" if trade["otc"] else "")
                )
                return trade

        logger.error(f"IQ Option trade failed for {symbol} (both regular and OTC)")
        return None

    # ── Result ────────────────────────────────────────────────────────────

    def wait_for_result(self, trade: dict, check_interval: int = 5, max_retries: int = 12) -> dict | None:
        """
        Wait for trade expiry then poll for WIN/LOSS result.
        Returns result dict with outcome, profit, new balance.
        """
        expiry_epoch = trade["expiry_epoch"]
        now          = time.time()
        wait_secs    = (expiry_epoch + 10) - now   # 10-second buffer after expiry

        if wait_secs > 0:
            logger.info(f"Waiting {wait_secs:.0f}s for trade {trade['trade_id']} to expire ...")
            time.sleep(wait_secs)

        for attempt in range(max_retries):
            try:
                profit = self.iq.check_win_v3(trade["trade_id"])

                if profit is not None:
                    outcome = "WIN" if float(profit) > 0 else "LOSS"
                    balance = self.refresh_balance()

                    result = {
                        "outcome":     outcome,
                        "profit":      float(profit),
                        "stake":       trade["stake"],
                        "payout":      trade["stake"] + float(profit) if float(profit) > 0 else 0.0,
                        "entry_spot":  0.0,
                        "exit_spot":   0.0,
                        "balance":     balance,
                        "currency":    "USD",
                        "contract_id": trade["trade_id"],
                    }

                    logger.info(
                        f"RESULT: {trade['symbol']} {trade['direction']} → {outcome} | "
                        f"Profit: USD {float(profit):+.2f} | Balance: USD {balance:.2f}"
                    )
                    return result

                logger.debug(f"Trade {trade['trade_id']} not settled yet (attempt {attempt+1}/{max_retries})")
                time.sleep(check_interval)

            except Exception as e:
                logger.error(f"IQ Option result check error: {e}")
                time.sleep(check_interval)

        logger.error(f"Could not retrieve result for trade {trade['trade_id']} after {max_retries} attempts")
        return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _buy(self, ticker: str, action: str, amount: float, duration: int):
        try:
            status, trade_id = self.iq.buy(amount, ticker, action, duration)
            if not status:
                logger.warning(f"IQ buy rejected: {ticker} {action} — status={status} id={trade_id}")
            return status, trade_id
        except Exception as e:
            logger.warning(f"IQ buy error ({ticker}): {e}")
            return False, None
