"""
Backtesting engine for the trend-following strategy.

Usage:
    python backtest.py                    # backtest all pairs, 60d history
    python backtest.py --symbol EURUSD=X  # single pair
    python backtest.py --period 90d       # custom lookback
"""

import argparse
import logging
import sys
from datetime import datetime

import pandas as pd

from config import TRADING_PAIRS, TRADE_AMOUNT_PCT, MAX_TRADE_AMOUNT, MIN_TRADE_AMOUNT
from data.fetcher import fetch_ohlcv
from data.indicators import add_all_indicators
from strategy.trend_following import TrendFollowingStrategy

logging.basicConfig(level=logging.WARNING)

PAYOUT = 0.82       # Typical Pocket Option payout (82% profit on win)
EXPIRY_CANDLES = 1  # Evaluate result on the next candle's close


def backtest_symbol(symbol: str, df: pd.DataFrame, payout: float = PAYOUT) -> dict:
    strategy = TrendFollowingStrategy()
    df = df.copy()

    balance = 1000.0
    initial = balance
    trades = []

    for i in range(1, len(df) - EXPIRY_CANDLES):
        window = df.iloc[: i + 1]
        sig = strategy.analyze(symbol, window)

        if sig.direction == "NONE":
            continue

        entry_price = df.iloc[i]["close"]
        exit_price = df.iloc[i + EXPIRY_CANDLES]["close"]

        amount = max(MIN_TRADE_AMOUNT, min(MAX_TRADE_AMOUNT, balance * TRADE_AMOUNT_PCT))

        if sig.direction == "BUY":
            won = exit_price > entry_price
        else:
            won = exit_price < entry_price

        if won:
            pnl = amount * payout
            balance += pnl
            outcome = "WIN"
        else:
            pnl = -amount
            balance -= amount
            outcome = "LOSS"

        trades.append({
            "time": df.index[i],
            "symbol": symbol,
            "direction": sig.direction,
            "strength": sig.strength,
            "entry": entry_price,
            "exit": exit_price,
            "amount": amount,
            "outcome": outcome,
            "pnl": pnl,
            "balance": balance,
        })

    if not trades:
        return {"symbol": symbol, "trades": 0}

    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades["outcome"] == "WIN"]
    losses = df_trades[df_trades["outcome"] == "LOSS"]

    win_rate = len(wins) / len(df_trades)
    net_pnl = df_trades["pnl"].sum()
    roi = (balance - initial) / initial

    # Max drawdown
    running_max = df_trades["balance"].cummax()
    drawdown = (df_trades["balance"] - running_max) / running_max
    max_dd = drawdown.min()

    return {
        "symbol": symbol,
        "trades": len(df_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "roi": roi,
        "max_drawdown": max_dd,
        "final_balance": balance,
        "df_trades": df_trades,
    }


def print_results(results: list[dict]):
    print("\n" + "=" * 70)
    print(f"  BACKTEST RESULTS  |  Payout: {PAYOUT:.0%}  |  Break-even: {1/(1+PAYOUT):.1%}")
    print("=" * 70)
    fmt = "{:<14} {:>6} {:>7} {:>9} {:>9} {:>9} {:>9}"
    print(fmt.format("Symbol", "Trades", "WinRate", "Net PnL", "ROI", "MaxDD", "Balance"))
    print("-" * 70)

    for r in results:
        if r.get("trades", 0) == 0:
            print(fmt.format(r["symbol"], "0", "-", "-", "-", "-", "-"))
            continue
        print(fmt.format(
            r["symbol"],
            r["trades"],
            f"{r['win_rate']:.1%}",
            f"${r['net_pnl']:+.2f}",
            f"{r['roi']:+.1%}",
            f"{r['max_drawdown']:.1%}",
            f"${r['final_balance']:.2f}",
        ))

    combined = [r for r in results if r.get("trades", 0) > 0]
    if combined:
        total_trades = sum(r["trades"] for r in combined)
        total_wins = sum(r["wins"] for r in combined)
        total_pnl = sum(r["net_pnl"] for r in combined)
        print("-" * 70)
        print(fmt.format(
            "TOTAL",
            total_trades,
            f"{total_wins/total_trades:.1%}",
            f"${total_pnl:+.2f}",
            "",
            "",
            "",
        ))
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Backtest the trend-following strategy")
    parser.add_argument("--symbol", default=None, help="Single symbol to test (e.g. EURUSD=X)")
    parser.add_argument("--period", default="60d", help="Lookback period (default: 60d)")
    parser.add_argument("--interval", default="5m", help="Candle interval (default: 5m)")
    parser.add_argument("--payout", type=float, default=PAYOUT, help=f"Broker payout (default: {PAYOUT})")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else TRADING_PAIRS
    results = []

    for sym in symbols:
        print(f"Fetching {sym} [{args.interval}, {args.period}] ...")
        df = fetch_ohlcv(sym, interval=args.interval, period=args.period)
        if df is None:
            print(f"  No data for {sym}, skipping.")
            results.append({"symbol": sym, "trades": 0})
            continue
        df = add_all_indicators(df)
        r = backtest_symbol(sym, df, payout=args.payout)
        results.append(r)

    print_results(results)

    # Save detailed trade log
    all_trades = pd.concat(
        [r["df_trades"] for r in results if "df_trades" in r], ignore_index=True
    )
    if not all_trades.empty:
        out = f"logs/backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        all_trades.to_csv(out, index=False)
        print(f"Detailed trade log saved to: {out}")


if __name__ == "__main__":
    main()
