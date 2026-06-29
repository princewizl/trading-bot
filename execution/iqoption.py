"""
IQ Option trading client.

Approach that produced the first confirmed trade (GBPJPY-OTC, id=14031127999):
  - Simple connect with hard threading timeout
  - Try regular symbol first, fall back to OTC automatically
  - No extra API calls (no get_all_open_time) — keep it minimal
"""

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# yfinance symbol → IQ Option ticker
IQ_SYMBOLS = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "USDCAD=X": "USDCAD",
    "EURGBP=X": "EURGBP",
    "GBPJPY=X": "GBPJPY",
}

CONNECT_TIMEOUT = 45   # seconds — covers IQ_Option() constructor + WebSocket handshake


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
        """
        Connect with a hard timeout. The IQ_Option constructor AND connect()
        both run inside a daemon thread so the main process never hangs.
        """
        slot = [None, None, None]   # [ok, reason, iq_instance]

        def _worker():
            try:
                from iqoptionapi.stable_api import IQ_Option
                iq = IQ_Option(self.email, self.password)
                ok, reason = iq.connect()
                slot[0] = bool(ok)
                slot[1] = reason
                slot[2] = iq
            except Exception as e:
                slot[1] = str(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=CONNECT_TIMEOUT)

        if t.is_alive() or slot[2] is None:
            logger.error(
                f"IQ Option connection timed out after {CONNECT_TIMEOUT}s "
                "(server may be blocking this IP — signal-only mode for this scan)"
            )
            return False

        ok, reason, iq_instance = slot
        if not ok:
            logger.error(f"IQ Option login failed: {reason}")
            return False

        self.iq = iq_instance
        account = "PRACTICE" if self.demo else "REAL"
        self.iq.change_balance(account)
        self.balance = self._safe_get_balance()

        logger.info(
            f"IQ Option connected ({'DEMO' if self.demo else 'LIVE'}) | "
            f"Balance: USD {self.balance:.2f}"
        )
        return True

    def disconnect(self):
        try:
            if self.iq:
                self.iq.close()
        except Exception:
            pass

    # ── Balance ───────────────────────────────────────────────────────────

    def refresh_balance(self) -> float:
        self.balance = self._safe_get_balance()
        return self.balance

    def _safe_get_balance(self) -> float:
        try:
            return float(self.iq.get_balance())
        except Exception:
            return self.balance

    # ── Trade placement ───────────────────────────────────────────────────

    def place_trade(self, symbol: str, direction: str, amount: float, duration_minutes: int = 5) -> dict | None:
        """
        Place a Higher/Lower binary option.
        Tries the regular market first, automatically falls back to OTC.
        """
        action = "call" if direction == "BUY" else "put"
        amount = round(amount, 2)

        for ticker in [symbol, f"{symbol}-OTC"]:
            status, trade_id = self._buy(ticker, action, amount, duration_minutes)
            if status:
                expiry_epoch = int(time.time()) + (duration_minutes * 60)
                trade = {
                    "trade_id":     trade_id,
                    "symbol":       ticker,
                    "yf_symbol":    symbol,
                    "direction":    direction,
                    "action":       action,
                    "stake":        amount,
                    "expiry_epoch": expiry_epoch,
                    "expiry_dt":    datetime.fromtimestamp(expiry_epoch, tz=timezone.utc).isoformat(),
                    "currency":     "USD",
                    "placed_at":    datetime.now(timezone.utc).isoformat(),
                    "otc":          ticker.endswith("-OTC"),
                }
                logger.info(
                    f"TRADE PLACED: {ticker} {direction} USD {amount:.2f} "
                    f"| id={trade_id} | expires={trade['expiry_dt']}"
                    + (" [OTC]" if trade["otc"] else "")
                )
                return trade

        logger.warning(f"Trade failed for {symbol} — pair may be closed on IQ Option right now")
        return None

    # ── Result ────────────────────────────────────────────────────────────

    def wait_for_result(self, trade: dict, check_interval: int = 5, max_retries: int = 12) -> dict | None:
        """Wait for expiry then poll for WIN/LOSS."""
        expiry_epoch = trade["expiry_epoch"]
        wait_secs    = (expiry_epoch + 10) - time.time()

        if wait_secs > 0:
            logger.info(f"Waiting {wait_secs:.0f}s for trade {trade['trade_id']} to expire ...")
            time.sleep(wait_secs)

        for attempt in range(max_retries):
            try:
                profit = self.iq.check_win_v3(trade["trade_id"])
                if profit is not None:
                    outcome = "WIN" if float(profit) > 0 else "LOSS"
                    balance = self.refresh_balance()
                    result  = {
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

                logger.debug(f"Trade {trade['trade_id']} not settled yet ({attempt+1}/{max_retries})")
                time.sleep(check_interval)

            except Exception as e:
                logger.error(f"Result check error: {e}")
                time.sleep(check_interval)

        logger.error(f"Could not retrieve result for trade {trade['trade_id']}")
        return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _buy(self, ticker: str, action: str, amount: float, duration: int):
        try:
            status, trade_id = self.iq.buy(amount, ticker, action, duration)
            if not status:
                logger.warning(f"IQ buy rejected: {ticker} {action} (pair may be closed)")
            return status, trade_id
        except Exception as e:
            logger.warning(f"IQ buy error ({ticker}): {e}")
            return False, None
