"""
Pocket Option execution layer.

Pocket Option has no official public API. This module provides two modes:

1. SIGNAL MODE (default, always safe):
   Prints signals to the terminal. You place trades manually on the website.

2. AUTO MODE (requires pocketoptionapi — unofficial WebSocket library):
   Connects via the platform's WebSocket to place trades automatically.
   You must extract your session ID (SSID) from browser DevTools → Application → Cookies.
   Auto mode works on demo and live accounts.

   Install:  pip install pocketoptionapi

DISCLAIMER: Automated trading on Pocket Option may violate their ToS.
Use auto mode only on a demo account until you have verified results.
"""

import logging
import time
from config import TRADING_MODE, POCKET_OPTION_SSID, PO_ASSET_MAP, TRADE_EXPIRY_SECONDS

logger = logging.getLogger(__name__)


class PocketOptionExecutor:
    def __init__(self):
        self.api = None
        self._connected = False

        if TRADING_MODE in ("demo", "live"):
            self._connect()

    # ── Connection ────────────────────────────────────────────────────────

    def _connect(self):
        if not POCKET_OPTION_SSID:
            logger.error("PO_SSID not set in .env  →  cannot connect to Pocket Option. Falling back to signal mode.")
            return

        try:
            from pocketoptionapi.stable_api import PocketOption
            demo = (TRADING_MODE == "demo")
            logger.info(f"Connecting to Pocket Option ({'DEMO' if demo else 'LIVE'}) ...")
            self.api = PocketOption(POCKET_OPTION_SSID, demo)
            check, reason = self.api.connect()
            if check:
                self._connected = True
                logger.info("Pocket Option connected successfully.")
            else:
                logger.error(f"Connection failed: {reason}")
        except ImportError:
            logger.error(
                "pocketoptionapi not installed. Run:  pip install pocketoptionapi\n"
                "Falling back to signal-only mode."
            )
        except Exception as e:
            logger.error(f"Unexpected connection error: {e}")

    # ── Place Trade ───────────────────────────────────────────────────────

    def place_trade(self, symbol: str, direction: str, amount: float) -> bool:
        """
        Place a binary options trade.
        direction: "BUY" (call) or "SELL" (put)
        Returns True if order was placed (or printed in signal mode).
        """
        if TRADING_MODE == "signal":
            return self._signal_only(symbol, direction, amount)

        if not self._connected:
            logger.warning("Not connected to Pocket Option. Printing signal instead.")
            return self._signal_only(symbol, direction, amount)

        asset = PO_ASSET_MAP.get(symbol, symbol)
        action = "call" if direction == "BUY" else "put"

        try:
            status, trade_id = self.api.buy(amount, asset, action, TRADE_EXPIRY_SECONDS)
            if status:
                logger.info(f"TRADE PLACED  {direction} {asset}  ${amount:.2f}  [{TRADE_EXPIRY_SECONDS}s]  ID:{trade_id}")
                return True
            else:
                logger.error(f"Trade rejected by platform: {trade_id}")
                return False
        except Exception as e:
            logger.error(f"Error placing trade: {e}")
            return False

    def check_result(self, trade_id) -> str | None:
        """
        Poll for trade result. Returns 'WIN', 'LOSS', or None if still open.
        Only works in auto mode.
        """
        if not self._connected or self.api is None:
            return None
        try:
            result = self.api.check_win_v4(trade_id)
            if result is None:
                return None
            profit = float(result)
            return "WIN" if profit > 0 else "LOSS"
        except Exception as e:
            logger.debug(f"check_result error: {e}")
            return None

    def get_balance(self) -> float | None:
        if self._connected and self.api:
            try:
                return float(self.api.get_balance())
            except Exception:
                pass
        return None

    def disconnect(self):
        if self._connected and self.api:
            try:
                self.api.disconnect()
            except Exception:
                pass

    # ── Signal-only fallback ──────────────────────────────────────────────

    @staticmethod
    def _signal_only(symbol: str, direction: str, amount: float) -> bool:
        asset = PO_ASSET_MAP.get(symbol, symbol)
        expiry_min = TRADE_EXPIRY_SECONDS // 60
        arrow = "↑ CALL" if direction == "BUY" else "↓ PUT"
        logger.info(
            f"\n{'='*52}\n"
            f"  MANUAL TRADE SIGNAL\n"
            f"  Asset   : {asset}\n"
            f"  Action  : {arrow}\n"
            f"  Amount  : ${amount:.2f}\n"
            f"  Expiry  : {expiry_min} minute(s)\n"
            f"{'='*52}"
        )
        return True
