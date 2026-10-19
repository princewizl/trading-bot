"""
Deriv broker for the long-running bot service — real-market forex Rise/Fall
on the same price feed the strategy reads (data/deriv_feed.py).

Same interface as execution/iq_broker.py, so bot_service.py doesn't care
which one it drives (config.BROKER).

Auth (new Deriv API): a Personal Access Token + registered App ID buy a
single-use OTP websocket URL for one account:
    POST https://api.derivws.com/trading/v1/options/accounts/{account}/otp
    → wss://api.derivws.com/trading/v1/options/ws/{demo|real}?otp=...

Every order: proposal (exact quote incl. payout) → payout check → buy that
proposal id at that price. Results are read per contract id with
proposal_open_contract (is_sold / status won|lost / profit) — exact, any
number of open contracts.

Env: DERIV_API_TOKEN, DERIV_TRADE_APP_ID, DERIV_ACCOUNT_ID
"""

import itertools
import json
import logging
import re
import time

import requests
import websocket

logger = logging.getLogger(__name__)

API            = "https://api.derivws.com/trading/v1/options"
CALL_TIMEOUT   = 15
PING_EVERY     = 30      # seconds of silence before a keepalive ping
CACHE_SECONDS  = 1800    # market-open catalog refresh
AUTH_BACKOFF   = 1800    # after the token / app id is rejected
MAX_BACKOFF    = 300


class DerivBroker:
    name = "Deriv"

    def __init__(self, token: str, app_id: str, account_id: str):
        self.token      = token
        self.app_id     = app_id
        self.account_id = account_id
        self.balance    = 0.0
        self.currency   = "USD"
        self.auth_rejected = False
        self.is_demo = True
        self.ws = None
        self._req_ids = itertools.count(1)
        self._last_io = 0.0
        self._next_attempt = 0.0
        self._fail_streak = 0
        self._catalog_at = 0.0
        self._open: dict[str, bool] = {}     # frx symbol -> tradeable now
        self._payouts: dict[str, float] = {} # last quoted payout fraction per symbol

    # ── Connection ────────────────────────────────────────────────────────

    def connected(self) -> bool:
        if self.ws is None:
            return False
        if time.time() - self._last_io > PING_EVERY:
            try:
                self._ask({"ping": 1}, timeout=10)
            except Exception as e:
                logger.warning(f"Deriv keepalive failed ({e}) — will reconnect")
                self.close()
                return False
        return True

    def ensure_connected(self) -> bool:
        if self.connected():
            return True
        if time.time() < self._next_attempt:
            return False
        if self._connect():
            self._fail_streak, self.auth_rejected = 0, False
            return True
        self._fail_streak += 1
        delay = AUTH_BACKOFF if self.auth_rejected else min(MAX_BACKOFF, 10 * 2 ** (self._fail_streak - 1))
        self._next_attempt = time.time() + delay
        logger.warning(f"Next Deriv connect attempt in {delay}s")
        return False

    def _connect(self) -> bool:
        try:
            r = requests.post(
                f"{API}/accounts/{self.account_id}/otp",
                headers={"Authorization": f"Bearer {self.token}", "Deriv-App-ID": self.app_id},
                timeout=20,
            )
        except Exception as e:
            logger.error(f"Deriv OTP request failed: {e}")
            return False
        if r.status_code in (401, 403):
            self.auth_rejected = True
            logger.error(f"Deriv rejected the API token / app id ({r.status_code}): {r.text[:200]}")
            return False
        if not r.ok:
            logger.error(f"Deriv OTP error {r.status_code}: {r.text[:200]}")
            return False
        try:
            url = r.json()["data"]["url"]
            self.ws = websocket.create_connection(url, timeout=20)
            self._last_io = time.time()
        except Exception as e:
            logger.error(f"Deriv websocket connect failed: {e}")
            self.ws = None
            return False
        self.refresh_balance()
        self._catalog_at = 0.0
        self.is_demo = "/ws/demo" in url
        mode = "DEMO" if self.is_demo else "REAL"
        logger.info(f"Deriv connected ({mode}) | {self.account_id} | Balance: {self.currency} {self.balance:.2f}")
        return True

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    # ── Account / catalog ─────────────────────────────────────────────────

    def refresh_balance(self) -> float:
        try:
            b = self._ask({"balance": 1})["balance"]
            self.balance, self.currency = float(b["balance"]), b.get("currency", self.currency)
        except Exception as e:
            logger.warning(f"Deriv balance read failed: {e}")
        return self.balance

    def symbol_for(self, yf_symbol: str) -> str | None:
        import config
        return config.DERIV_SYMBOLS.get(yf_symbol)

    def ticker_for(self, symbol: str) -> tuple[str | None, str]:
        self._refresh_catalog()
        if not self._open or self._open.get(symbol, True):
            return symbol, ""
        return None, f"{symbol} market closed or suspended on Deriv right now"

    def payout(self, ticker: str) -> float | None:
        return self._payouts.get(ticker)

    def availability(self, symbols: list[str]) -> dict[str, str]:
        self._refresh_catalog()
        out = {}
        for s in symbols:
            sym = s if s.startswith("frx") else f"frx{s}"
            state = "open" if self._open.get(sym) else ("closed" if sym in self._open else "unknown")
            pay = f" | last payout {self._payouts[sym]:.0%}" if sym in self._payouts else ""
            out[s] = f"{sym}: {state}{pay}"
        return out

    def _refresh_catalog(self) -> None:
        if time.time() - self._catalog_at < CACHE_SECONDS:
            return
        self._catalog_at = time.time()
        try:
            rows = self._ask({"active_symbols": "brief"})["active_symbols"]
            self._open = {
                (r.get("underlying_symbol") or r.get("symbol")): bool(r.get("exchange_is_open")) and not r.get("is_trading_suspended")
                for r in rows if str(r.get("underlying_symbol") or r.get("symbol") or "").startswith("frx")
            }
        except Exception as e:
            logger.warning(f"Deriv market catalog unavailable ({e}) — will try placements anyway")

    # ── Orders ────────────────────────────────────────────────────────────

    def buy(self, ticker: str, direction: str, amount: float, minutes: int,
            min_payout: float = 0.0) -> tuple[dict | None, str]:
        """Quote, check payout, buy. Returns ({order_id, expiry, payout}, "") or (None, reason)."""
        contract = "CALL" if direction == "BUY" else "PUT"
        quote = {"proposal": 1, "contract_type": contract, "currency": self.currency,
                 "duration": minutes, "duration_unit": "m", "underlying_symbol": ticker}
        try:
            q = self._ask({**quote, "amount": round(amount, 2), "basis": "stake"})
            cap = _max_payout(q)
            if cap is not None:
                # Deriv caps the payout per contract (e.g. $100 on demo intraday forex), so a
                # 2%-of-balance stake can be refused outright. Re-quote at the cap and let
                # Deriv size the stake; the payout check below still applies.
                logger.info(f"{ticker}: stake {amount:.2f} exceeds Deriv's max payout {cap:.2f} — re-quoting at the cap")
                q = self._ask({**quote, "amount": cap, "basis": "payout"})
        except Exception as e:
            return None, f"Deriv quote failed: {e}"
        if "error" in q:
            return None, f"Deriv quote rejected: {q['error'].get('message')}"
        p = q["proposal"]
        stake = float(p["ask_price"])
        payout = float(p["payout"]) / stake - 1 if stake else 0.0
        self._payouts[ticker] = payout
        if payout < min_payout:
            return None, f"payout {payout:.0%} < minimum {min_payout:.0%}"

        try:
            b = self._ask({"buy": p["id"], "price": stake})
        except Exception as e:
            return None, f"Deriv buy failed: {e}"
        if "error" in b:
            return None, f"Deriv buy rejected: {b['error'].get('message')}"
        receipt = b["buy"]
        self.balance = float(receipt.get("balance_after", self.balance))
        start = int(receipt.get("start_time") or receipt.get("purchase_time") or time.time())
        return {"order_id": str(receipt["contract_id"]), "expiry": start + minutes * 60,
                "payout": payout, "stake": float(receipt.get("buy_price", stake))}, ""

    def pushed_result(self, order_id: str, stake: float, ticker: str) -> tuple[str, float | None, str] | None:
        """Settled result for one contract, or None while it's still open."""
        try:
            c = self._ask({"proposal_open_contract": 1, "contract_id": int(order_id)})
        except Exception as e:
            logger.warning(f"Deriv contract {order_id} lookup failed: {e}")
            return None
        if "error" in c:
            logger.warning(f"Deriv contract {order_id}: {c['error'].get('message')}")
            return None
        poc = c.get("proposal_open_contract") or {}
        if not poc.get("is_sold"):
            return None
        status = {"won": "WIN", "lost": "LOSS"}.get(poc.get("status"), "UNKNOWN")
        buy_price = float(poc.get("buy_price") or stake)
        sell_price = float(poc.get("sell_price") or 0)
        profit = round(sell_price - buy_price, 2)
        if status == "UNKNOWN" and abs(profit) < 1e-9:
            status = "TIE"
        return status, profit, "contract"

    def history_results(self, limit: int = 50) -> dict:
        return {}   # pushed_result reads any contract by id, including after a restart

    # ── Internals ─────────────────────────────────────────────────────────

    def _ask(self, payload: dict, timeout: float = CALL_TIMEOUT) -> dict:
        if self.ws is None:
            raise ConnectionError("not connected")
        req_id = next(self._req_ids)
        try:
            self.ws.send(json.dumps({**payload, "req_id": req_id}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                self.ws.settimeout(max(0.1, deadline - time.time()))
                msg = json.loads(self.ws.recv())
                if msg.get("req_id") == req_id:
                    self._last_io = time.time()
                    return msg
            raise TimeoutError(f"no reply to {next(iter(payload))}")
        except Exception:
            self.close()
            raise


def _max_payout(resp: dict) -> float | None:
    """The payout cap from a 'maximum payout of X' quote rejection, else None."""
    err = resp.get("error") or {}
    details = err.get("details") or {}
    for key in ("max_payout", "maximum_payout"):
        if isinstance(details, dict) and details.get(key):
            return float(details[key])
    m = re.search(r"maximum payout of ([\d,]+(?:\.\d+)?)", err.get("message") or "")
    return float(m.group(1).replace(",", "")) if m else None
