import csv
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from config import (
    CURRENCY,
    ACCOUNT_BALANCE,
    TRADE_AMOUNT_PCT,
    MAX_TRADE_AMOUNT,
    MIN_TRADE_AMOUNT,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS_PCT,
    MAX_DAILY_SIGNALS as MAX_DAILY_TRADES,
    MARTINGALE_ENABLED,
    MARTINGALE_MULTIPLIER,
    MARTINGALE_MAX_STEPS,
    TRADE_LOG_FILE,
)

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, balance: float = ACCOUNT_BALANCE):
        self.balance = balance
        self.initial_balance = balance
        self.daily_start_balance = balance

        self.consecutive_losses = 0
        self.martingale_step = 0

        self.today_trades: list[dict] = []
        self.all_trades: list[dict] = []
        self._last_reset_date: date = date.today()

        Path(TRADE_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        self._init_csv()

    # ── Position Sizing ───────────────────────────────────────────────────

    def get_trade_amount(self) -> float:
        if MARTINGALE_ENABLED and self.martingale_step > 0:
            base = self.balance * TRADE_AMOUNT_PCT
            amount = base * (MARTINGALE_MULTIPLIER ** self.martingale_step)
        else:
            amount = self.balance * TRADE_AMOUNT_PCT

        amount = max(MIN_TRADE_AMOUNT, min(MAX_TRADE_AMOUNT, amount))
        return round(amount, 2)

    # ── Gate Checks (call before placing a trade) ─────────────────────────

    def can_trade(self) -> tuple[bool, str]:
        self._daily_reset_if_needed()

        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return False, f"Max consecutive losses reached ({MAX_CONSECUTIVE_LOSSES}). Pausing."

        daily_loss = self.daily_start_balance - self.balance
        if daily_loss >= self.daily_start_balance * MAX_DAILY_LOSS_PCT:
            return False, f"Daily loss limit hit ({MAX_DAILY_LOSS_PCT:.0%} of day-start balance)."

        if len(self.today_trades) >= MAX_DAILY_TRADES:
            return False, f"Daily trade limit reached ({MAX_DAILY_TRADES} trades)."

        if self.balance < MIN_TRADE_AMOUNT:
            return False, "Balance too low to trade."

        return True, "OK"

    # ── Trade Recording ───────────────────────────────────────────────────

    def record_win(self, symbol: str, amount: float, payout: float):
        profit = amount * payout
        self.balance += profit
        self.consecutive_losses = 0
        self.martingale_step = 0
        self._record(symbol, "WIN", amount, profit)
        logger.info(f"WIN  {symbol}  +{CURRENCY}{profit:.2f}  |  Balance: {CURRENCY}{self.balance:.2f}")

    def record_loss(self, symbol: str, amount: float):
        self.balance -= amount
        self.consecutive_losses += 1
        if MARTINGALE_ENABLED:
            self.martingale_step = min(self.martingale_step + 1, MARTINGALE_MAX_STEPS)
        self._record(symbol, "LOSS", amount, -amount)
        logger.info(f"LOSS {symbol}  -{CURRENCY}{amount:.2f}  |  Balance: {CURRENCY}{self.balance:.2f}  |  Streak: {self.consecutive_losses}")

    # ── Statistics ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        if not self.all_trades:
            return {}
        wins = [t for t in self.all_trades if t["outcome"] == "WIN"]
        losses = [t for t in self.all_trades if t["outcome"] == "LOSS"]
        total = len(self.all_trades)
        win_rate = len(wins) / total if total else 0
        net_pnl = sum(t["pnl"] for t in self.all_trades)
        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "balance": self.balance,
            "roi": (self.balance - self.initial_balance) / self.initial_balance,
            "max_streak_loss": self.consecutive_losses,
        }

    # ── Internals ─────────────────────────────────────────────────────────

    def _record(self, symbol: str, outcome: str, amount: float, pnl: float):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "outcome": outcome,
            "amount": amount,
            "pnl": pnl,
            "balance": self.balance,
        }
        self.today_trades.append(entry)
        self.all_trades.append(entry)
        self._append_csv(entry)

    def _daily_reset_if_needed(self):
        today = date.today()
        if today != self._last_reset_date:
            self.daily_start_balance = self.balance
            self.today_trades = []
            self._last_reset_date = today
            logger.info("Daily counters reset.")

    def _init_csv(self):
        p = Path(TRADE_LOG_FILE)
        if not p.exists():
            with open(p, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "outcome", "amount", "pnl", "balance"])
                writer.writeheader()

    def _append_csv(self, entry: dict):
        with open(TRADE_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "outcome", "amount", "pnl", "balance"])
            writer.writerow(entry)
