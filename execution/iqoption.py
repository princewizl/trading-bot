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
        self.email        = email
        self.password     = password
        self.demo         = demo
        self.iq           = None
        self.balance      = 0.0
        self.currency     = "USD"
        self.open_symbols: set[str] | None = None   # populated after connect()

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
        self.open_symbols = self._fetch_open_symbols()

        open_count = len(self.open_symbols) if self.open_symbols is not None else "?"
        logger.info(
            f"IQ Option connected ({'DEMO' if self.demo else 'LIVE'}) | "
            f"Balance: USD {self.balance:.2f} | "
            f"Open assets: {open_count}"
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

    def place_trade(
        self,
        symbol: str,
        direction: str,
        amount: float,
        duration_minutes: int = 5,
        otc_stake_pct: float = 0.80,
    ) -> dict | None:
        """
        Place a Higher/Lower binary option.
        Tries the regular market first; falls back to OTC at 80% stake.
        OTC markets have lower payouts (61–75%) so we risk less on them.
        """
        action = "call" if direction == "BUY" else "put"

        for ticker in [symbol, f"{symbol}-OTC"]:
            is_otc  = ticker.endswith("-OTC")
            # Reduce stake for OTC to compensate for lower payout
            stake   = round(amount * (otc_stake_pct if is_otc else 1.0), 2)
            stake   = max(stake, 1.0)   # never below IQ Option minimum

            status, trade_id, expiry_epoch = self._buy(ticker, action, stake, duration_minutes)
            if status:
                trade = {
                    "trade_id":      trade_id,
                    "symbol":        ticker,
                    "yf_symbol":     symbol,
                    "direction":     direction,
                    "action":        action,
                    "stake":         stake,
                    "balance_before": self.balance,
                    "expiry_epoch":  expiry_epoch,
                    "expiry_dt":     datetime.fromtimestamp(expiry_epoch, tz=timezone.utc).isoformat(),
                    "currency":      "USD",
                    "placed_at":     datetime.now(timezone.utc).isoformat(),
                    "otc":           is_otc,
                }
                otc_note = f" [OTC — stake reduced to {otc_stake_pct:.0%}]" if is_otc else ""
                logger.info(
                    f"TRADE PLACED: {ticker} {direction} USD {stake:.2f} "
                    f"| id={trade_id} | expires={trade['expiry_dt']}{otc_note}"
                )
                return trade

        logger.warning(f"Trade failed for {symbol} — pair may be closed on IQ Option right now")
        return None

    # ── Result ────────────────────────────────────────────────────────────

    def wait_for_result(self, trade: dict, check_interval: int = 10, max_retries: int = 6) -> dict | None:
        """Single-trade convenience: wait for expiry then check result."""
        self._wait_until(trade["expiry_epoch"], label=str(trade["trade_id"]))
        result = self.check_result(trade, check_interval, max_retries)
        return result if result is not None else self._result_from_balance(trade)

    def wait_for_all(self, trades: list[dict]) -> None:
        """Wait until the last trade in the batch has expired."""
        if not trades:
            return
        latest = max(t["expiry_epoch"] for t in trades)
        ids    = ", ".join(str(t["trade_id"]) for t in trades)
        self._wait_until(latest, label=f"{len(trades)} trade(s) [{ids}]")

    def check_result(self, trade: dict, check_interval: int = 10, max_retries: int = 6) -> dict | None:
        """
        Poll for a result without waiting — call AFTER expiry has passed.
        Returns None if check_win_v3 is unavailable; caller handles fallback.
        """
        logger.info(f"Trade {trade['trade_id']} expired — checking result ...")
        for attempt in range(max_retries):
            profit = self._check_win(trade["trade_id"])
            if profit is not None:
                return self._build_result(trade, float(profit))
            logger.info(f"check_win_v3 not ready — attempt {attempt+1}/{max_retries} ...")
            time.sleep(check_interval)
        logger.info(f"check_win_v3 unavailable for trade {trade['trade_id']}")
        return None

    def _wait_until(self, expiry_epoch: int, label: str = "") -> None:
        """Sleep until expiry_epoch + 10s buffer, logging every 30s."""
        wait_secs = (expiry_epoch + 10) - time.time()
        if wait_secs <= 0:
            return
        end_time = time.time() + wait_secs
        while time.time() < end_time:
            remaining = int(end_time - time.time())
            logger.info(f"{label} — {remaining}s until expiry ...")
            time.sleep(min(30, max(1, remaining)))

    def _build_result(self, trade: dict, profit: float) -> dict:
        balance = self.refresh_balance()
        outcome = "WIN" if profit > 0 else "LOSS"
        result  = {
            "outcome":     outcome,
            "profit":      profit,
            "stake":       trade["stake"],
            "payout":      trade["stake"] + profit if profit > 0 else 0.0,
            "entry_spot":  0.0,
            "exit_spot":   0.0,
            "balance":     balance,
            "currency":    "USD",
            "contract_id": trade["trade_id"],
        }
        logger.info(
            f"RESULT: {trade['symbol']} {trade['direction']} → {outcome} | "
            f"Profit: USD {profit:+.2f} | Balance: USD {balance:.2f}"
        )
        return result

    def _result_from_balance(self, trade: dict) -> dict | None:
        """
        Infer result from balance change. IQ Option settles immediately on expiry
        so the new balance always reflects the real outcome.
        """
        try:
            balance_before = trade.get("balance_before", self.balance)
            new_balance    = self.refresh_balance()
            net            = round(new_balance - balance_before, 2)

            if abs(net) < 0.01:
                logger.warning("Balance unchanged — result unknown (trade may still be processing)")
                return None

            # net > 0  → WIN  (IQ Option returned stake + profit)
            # net < 0  → LOSS (IQ Option kept stake)
            outcome = "WIN" if net > 0 else "LOSS"
            logger.info(
                f"RESULT (balance method): {trade['symbol']} {trade['direction']} → {outcome} | "
                f"Net: USD {net:+.2f} | Balance: USD {new_balance:.2f} "
                f"(was USD {balance_before:.2f})"
            )
            return {
                "outcome":     outcome,
                "profit":      net,
                "stake":       trade["stake"],
                "payout":      new_balance - balance_before + trade["stake"] if net > 0 else 0.0,
                "entry_spot":  0.0,
                "exit_spot":   0.0,
                "balance":     new_balance,
                "currency":    "USD",
                "contract_id": trade["trade_id"],
            }
        except Exception as e:
            logger.error(f"Balance-based result failed: {e}")
            return None

    def _check_win(self, trade_id) -> float | None:
        """check_win_v3 wrapped in a 15-second timeout so it never hangs."""
        result = [None]

        def _fetch():
            try:
                result[0] = self.iq.check_win_v3(trade_id)
            except Exception:
                pass

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=15)
        return result[0]

    # ── Internal ──────────────────────────────────────────────────────────

    def _fetch_open_symbols(self) -> set[str] | None:
        """
        Ask IQ Option which assets are currently open for binary/turbo trading.
        Returns a set of uppercase ticker strings (e.g. {'EURUSD', 'GBPUSD'}).
        Returns None if the call fails or times out — callers treat None as
        'unknown, try anyway' so we never silently skip a tradeable pair.
        """
        result = [None]

        def _fetch():
            try:
                data = self.iq.get_all_open_time()
                if not isinstance(data, dict):
                    return
                open_set: set[str] = set()
                for category in ("turbo", "binary", "digital"):
                    for symbol, info in data.get(category, {}).items():
                        if isinstance(info, dict) and info.get("open"):
                            open_set.add(symbol.upper().replace("-OTC", ""))
                            # also keep OTC variant as open if base is open
                result[0] = open_set
            except Exception as e:
                logger.debug(f"get_all_open_time failed: {e}")

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=10)
        return result[0]

    def _buy(self, ticker: str, action: str, amount: float, duration: int):
        """
        Returns (status, trade_id, expiry_epoch).

        Turbo only (duration-based, e.g. "5 minutes from now"). IQ Option's
        binary option type (the other expiry class this API exposes) is
        aligned to 15-minute clock boundaries, not 5 — riding a 5-minute
        confluence signal for 15-60 minutes doesn't match what the strategy
        was built for, so we don't fall back to it. If turbo is rejected
        (pair closed for short-duration options right now), the caller
        treats it as a failed placement and sends a signal-only email.
        """
        try:
            status, trade_id = self.iq.buy(amount, ticker, action, duration)
            if status:
                return True, trade_id, int(time.time()) + duration * 60
            logger.warning(f"IQ buy rejected: {ticker} {action} (pair may be closed for turbo right now)")
        except Exception as e:
            logger.warning(f"IQ buy error ({ticker}): {e}")

        return False, None, 0
