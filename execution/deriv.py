"""
Deriv.com WebSocket API client.

Handles authentication, trade placement (Rise/Fall contracts),
and result checking — all in one synchronous connection.

Setup:
  1. Create a free Deriv account at deriv.com
  2. Go to Account Settings → API Token → Create token
     Select permissions: Read + Trade
  3. Add DERIV_API_TOKEN=your_token to .env
  4. Set DERIV_DEMO=true to trade on virtual balance first

Deriv Rise/Fall contracts:
  - BUY signal → CALL (Rise)  — win if price is higher at expiry
  - SELL signal → PUT  (Fall) — win if price is lower at expiry
  - Payout: typically 80–95% profit on win, lose 100% of stake on loss
"""

import json
import logging
import time
from datetime import datetime, timezone

import websocket

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws.binaryws.com/websockets/v3?app_id={app_id}"


class DerivClient:
    def __init__(self, api_token: str, app_id: str = "1089", demo: bool = True):
        self.api_token  = api_token
        self.app_id     = app_id
        self.demo       = demo
        self.ws         = None
        self.balance    = 0.0
        self.currency   = "USD"
        self.login_id   = ""
        self._connected = False

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            url = _WS_URL.format(app_id=self.app_id)
            self.ws = websocket.create_connection(url, timeout=20)
            resp = self._request({"authorize": self.api_token}, "authorize")
            if resp is None:
                return False

            auth = resp["authorize"]
            self.login_id = auth.get("loginid", "")
            self.balance  = float(auth.get("balance", 0))
            self.currency = auth.get("currency", "USD")

            logger.info(
                f"Deriv connected: {self.login_id} "
                f"({'DEMO' if self.demo else 'LIVE'}) | "
                f"Balance: {self.currency} {self.balance:.2f}"
            )
            self._connected = True
            return True

        except Exception as e:
            logger.error(f"Deriv connection failed: {e}")
            return False

    def disconnect(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self._connected = False

    # ── Balance ───────────────────────────────────────────────────────────

    def refresh_balance(self) -> float:
        resp = self._request({"balance": 1, "account": "current"}, "balance")
        if resp:
            self.balance = float(resp["balance"]["balance"])
        return self.balance

    # ── Trade placement ───────────────────────────────────────────────────

    def place_trade(self, symbol: str, direction: str, amount: float, duration_minutes: int = 5) -> dict | None:
        """
        Place a Rise/Fall contract.
        direction: "BUY" (CALL/Rise) or "SELL" (PUT/Fall)

        Returns a trade record dict, or None if failed.
        """
        contract_type = "CALL" if direction == "BUY" else "PUT"
        amount = round(amount, 2)

        # Step 1 — get proposal (price + payout estimate)
        prop_resp = self._request({
            "proposal":       1,
            "amount":         str(amount),
            "basis":          "stake",
            "contract_type":  contract_type,
            "currency":       self.currency,
            "duration":       duration_minutes,
            "duration_unit":  "m",
            "symbol":         symbol,
        }, "proposal")

        if prop_resp is None:
            logger.error(f"Proposal failed for {symbol}")
            return None

        proposal     = prop_resp["proposal"]
        proposal_id  = proposal["id"]
        ask_price    = float(proposal["ask_price"])
        payout       = float(proposal["payout"])

        logger.info(f"Proposal: {symbol} {contract_type} stake={self.currency}{amount:.2f} payout={self.currency}{payout:.2f}")

        # Step 2 — buy the contract
        buy_resp = self._request({"buy": proposal_id, "price": str(amount)}, "buy")

        if buy_resp is None:
            logger.error(f"Buy failed for {symbol}")
            return None

        buy         = buy_resp["buy"]
        contract_id = buy["contract_id"]
        buy_price   = float(buy["buy_price"])
        expiry_time = int(buy["date_expiry"])

        self.balance -= buy_price   # update local estimate

        trade = {
            "contract_id":    contract_id,
            "symbol":         symbol,
            "direction":      direction,
            "contract_type":  contract_type,
            "stake":          buy_price,
            "potential_payout": payout,
            "expiry_epoch":   expiry_time,
            "expiry_dt":      datetime.fromtimestamp(expiry_time, tz=timezone.utc).isoformat(),
            "currency":       self.currency,
            "placed_at":      datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            f"TRADE PLACED: {symbol} {direction} "
            f"{self.currency} {buy_price:.2f} | "
            f"contract_id={contract_id} | "
            f"expires={trade['expiry_dt']}"
        )
        return trade

    # ── Result checking ───────────────────────────────────────────────────

    def wait_for_result(self, trade: dict, check_interval: int = 5, max_retries: int = 10) -> dict | None:
        """
        Wait until a contract's expiry then poll for the settled result.
        Returns result dict with outcome, profit, balance — or None if timed out.
        """
        expiry_epoch = trade["expiry_epoch"]
        now          = time.time()
        wait_secs    = (expiry_epoch + 15) - now   # 15s buffer after expiry

        if wait_secs > 0:
            logger.info(f"Waiting {wait_secs:.0f}s for contract {trade['contract_id']} to expire ...")
            time.sleep(wait_secs)

        # Poll until settled
        for attempt in range(max_retries):
            resp = self._request({
                "proposal_open_contract": 1,
                "contract_id": trade["contract_id"],
            }, "proposal_open_contract")

            if resp is None:
                time.sleep(check_interval)
                continue

            poc = resp["proposal_open_contract"]

            if poc.get("is_expired") or poc.get("is_sold") or poc.get("status") in ("won", "lost"):
                status  = poc.get("status", "unknown")
                profit  = float(poc.get("profit", 0))
                outcome = "WIN" if status == "won" else "LOSS"

                # Refresh real balance from Deriv
                real_balance = self.refresh_balance()

                result = {
                    "outcome":      outcome,
                    "status":       status,
                    "profit":       profit,
                    "stake":        trade["stake"],
                    "payout":       float(poc.get("sell_price", 0)),
                    "entry_spot":   float(poc.get("entry_spot", 0)),
                    "exit_spot":    float(poc.get("exit_tick", poc.get("current_spot", 0))),
                    "balance":      real_balance,
                    "currency":     self.currency,
                    "contract_id":  trade["contract_id"],
                }

                logger.info(
                    f"RESULT: {trade['symbol']} {trade['direction']} → {outcome} | "
                    f"Profit: {self.currency} {profit:+.2f} | "
                    f"Balance: {self.currency} {real_balance:.2f}"
                )
                return result

            logger.debug(f"Contract {trade['contract_id']} not yet settled (attempt {attempt+1}/{max_retries})")
            time.sleep(check_interval)

        logger.error(f"Could not get result for contract {trade['contract_id']} after {max_retries} attempts")
        return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _request(self, payload: dict, expected_msg_type: str, timeout: int = 15) -> dict | None:
        """Send a request and wait for the matching response type, discarding unrelated messages."""
        try:
            self.ws.send(json.dumps(payload))
            deadline = time.time() + timeout

            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.ws.settimeout(remaining)
                raw  = self.ws.recv()
                resp = json.loads(raw)

                if "error" in resp:
                    err = resp["error"]
                    logger.error(f"Deriv error [{expected_msg_type}]: {err.get('message', err)}")
                    return None

                if resp.get("msg_type") == expected_msg_type:
                    return resp
                # Heartbeat or unrelated message — keep waiting

            logger.error(f"Timeout waiting for '{expected_msg_type}' response")
            return None

        except Exception as e:
            logger.error(f"Deriv request error [{expected_msg_type}]: {e}")
            return None
