#!/usr/bin/env python3
"""
Backtest Spread Signal Strategy

Uses historical option chain data to test signal generation performance.

Usage:
    python scripts/backtest_signals.py --start-date 2024-01-01 --end-date 2024-12-31
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage
from analysis.signal_engine import generate_signal_report
from dataclasses import dataclass
from typing import List


@dataclass
class BacktestTrade:
    """Record of a backtest trade"""
    entry_date: datetime
    exit_date: datetime
    structure_type: str
    underlying: str
    entry_cost: float
    exit_value: float
    pnl: float
    return_pct: float
    days_held: int
    max_loss: float
    max_profit: float


@dataclass
class BacktestResults:
    """Overall backtest statistics"""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    total_pnl: float
    total_return_pct: float
    max_drawdown: float
    sharpe_ratio: float
    trades: List[BacktestTrade]


def simulate_spread_exit(
    entry_chain,
    exit_chain,
    candidate
) -> float:
    """
    Simulate closing a spread position.
    
    Returns exit value (how much you get back when closing).
    """
    # Find matching strikes in exit chain
    long_strike_data = None
    short_strike_data = None
    
    for strike in exit_chain.strikes:
        if strike.strike == candidate.long_strike:
            long_strike_data = strike
        if candidate.short_strike and strike.strike == candidate.short_strike:
            short_strike_data = strike
    
    if not long_strike_data:
        # Strike not found, assume worthless
        return 0.0
    
    # Exit pricing (reverse of entry: sell @ bid, buy @ ask)
    if candidate.structure_type in ["BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"]:
        # Vertical spread: sell the long leg, buy back the short leg
        if "CALL" in candidate.structure_type:
            long_exit = long_strike_data.call_bid or 0  # Sell long call at bid
            short_exit = short_strike_data.call_ask or 0 if short_strike_data else 0  # Buy back short call at ask
        else:  # PUT
            long_exit = long_strike_data.put_bid or 0
            short_exit = short_strike_data.put_ask or 0 if short_strike_data else 0
        
        net_credit = long_exit - short_exit
        exit_value = net_credit * candidate.lot_size
        
    else:  # Naked long (LONG_CALL, LONG_PUT)
        if "CALL" in candidate.structure_type:
            exit_price = long_strike_data.call_bid or 0
        else:
            exit_price = long_strike_data.put_bid or 0
        
        exit_value = exit_price * candidate.lot_size
    
    # Subtract exit transaction costs
    exit_costs = 125  # Approximate (4 orders for spread, 2 for naked)
    
    return max(exit_value - exit_costs, 0)


def run_backtest(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    storage: ChainStorage,
    hold_days: int = 3
) -> BacktestResults:
    """
    Run backtest on historical data.
    
    Strategy:
    1. Generate signal each day at market close
    2. Enter top-ranked spread next day at open
    3. Exit after hold_days or at expiry-1, whichever comes first
    4. Track P&L
    """
    print(f"\n🔬 Backtesting {symbol}")
    print(f"{'=' * 60}")
    print(f"Date range: {start_date.date()} to {end_date.date()}")
    print(f"Hold period: {hold_days} days")
    print()
    
    trades = []
    current_date = start_date
    
    while current_date <= end_date:
        # Skip weekends
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1)
            continue
        
        print(f"[{current_date.date()}] Generating signal...", end=" ")
        
        try:
            # Get historical chain for this date
            chains = storage.get_chains_on_date(symbol, current_date.date())
            
            if not chains:
                print("⚠️  No data")
                current_date += timedelta(days=1)
                continue
            
            # Use nearest weekly expiry
            chain = chains[0]  # Assume first chain is nearest expiry
            
            # Generate signal (using historical data, no look-ahead)
            report = generate_signal_report(
                underlying=symbol,
                expiry=chain.expiry,
                price_source="conservative"
            )
            
            # Get top-ranked spread
            if not report.top_spreads:
                print("❌ No valid spreads")
                current_date += timedelta(days=1)
                continue
            
            top_spread = report.top_spreads[0]
            
            # Calculate exit date
            expiry_exit = chain.expiry - timedelta(days=1)  # Exit 1 day before expiry
            time_exit = current_date + timedelta(days=hold_days)
            exit_date = min(expiry_exit, time_exit.date())
            
            # Get exit chain
            exit_chain_list = storage.get_chains_on_date(symbol, exit_date)
            
            if not exit_chain_list:
                print(f"⚠️  No exit data for {exit_date}")
                current_date += timedelta(days=1)
                continue
            
            exit_chain = exit_chain_list[0]
            
            # Simulate exit
            exit_value = simulate_spread_exit(chain, exit_chain, top_spread)
            entry_cost = top_spread.total_cost
            pnl = exit_value - entry_cost
            return_pct = (pnl / entry_cost) * 100 if entry_cost > 0 else 0
            
            # Record trade
            trade = BacktestTrade(
                entry_date=current_date,
                exit_date=datetime.combine(exit_date, datetime.min.time()),
                structure_type=top_spread.structure_type,
                underlying=symbol,
                entry_cost=entry_cost,
                exit_value=exit_value,
                pnl=pnl,
                return_pct=return_pct,
                days_held=(exit_date - current_date.date()).days,
                max_loss=top_spread.max_loss,
                max_profit=top_spread.max_profit or 0
            )
            
            trades.append(trade)
            
            status = "✅" if pnl > 0 else "❌"
            print(f"{status} P&L: ₹{pnl:.0f} ({return_pct:+.1f}%)")
            
        except Exception as e:
            print(f"❌ Error: {e}")
        
        current_date += timedelta(days=1)
    
    # Calculate statistics
    if not trades:
        print("\n❌ No trades executed")
        return None
    
    winning_trades = [t for t in trades if t.pnl > 0]
    losing_trades = [t for t in trades if t.pnl <= 0]
    
    win_rate = len(winning_trades) / len(trades) * 100
    avg_win = sum(t.pnl for t in winning_trades) / len(winning_trades) if winning_trades else 0
    avg_loss = sum(t.pnl for t in losing_trades) / len(losing_trades) if losing_trades else 0
    total_pnl = sum(t.pnl for t in trades)
    
    # Assuming ₹1L capital
    capital = 100000
    total_return = (total_pnl / capital) * 100
    
    # Calculate max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    
    for trade in trades:
        cumulative += trade.pnl
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_dd = max(max_dd, drawdown)
    
    max_dd_pct = (max_dd / capital) * 100
    
    # Sharpe ratio (simplified)
    returns = [t.return_pct for t in trades]
    avg_return = sum(returns) / len(returns)
    std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
    sharpe = (avg_return / std_return) if std_return > 0 else 0
    
    results = BacktestResults(
        total_trades=len(trades),
        winning_trades=len(winning_trades),
        losing_trades=len(losing_trades),
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        total_pnl=total_pnl,
        total_return_pct=total_return,
        max_drawdown=max_dd_pct,
        sharpe_ratio=sharpe,
        trades=trades
    )
    
    return results


def print_results(results: BacktestResults):
    """Print backtest results in readable format."""
    if not results:
        return
    
    print()
    print(f"{'=' * 60}")
    print(f"📊 BACKTEST RESULTS")
    print(f"{'=' * 60}")
    print()
    
    print(f"Total Trades:      {results.total_trades}")
    print(f"Winning Trades:    {results.winning_trades} ({results.win_rate:.1f}%)")
    print(f"Losing Trades:     {results.losing_trades}")
    print()
    
    print(f"Average Win:       ₹{results.avg_win:,.0f}")
    print(f"Average Loss:      ₹{results.avg_loss:,.0f}")
    print(f"Win/Loss Ratio:    {abs(results.avg_win / results.avg_loss):.2f}" if results.avg_loss != 0 else "N/A")
    print()
    
    print(f"Total P&L:         ₹{results.total_pnl:,.0f}")
    print(f"Total Return:      {results.total_return_pct:+.2f}%")
    print(f"Max Drawdown:      {results.max_drawdown:.2f}%")
    print(f"Sharpe Ratio:      {results.sharpe_ratio:.2f}")
    print()
    
    # Profit factor
    gross_profit = sum(t.pnl for t in results.trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in results.trades if t.pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    print(f"Profit Factor:     {profit_factor:.2f}")
    print()
    
    # Interpretation
    print(f"{'─' * 60}")
    print("💡 Interpretation:")
    print()
    
    if results.total_return_pct > 15:
        print("✅ Strong performance (>15% annual)")
    elif results.total_return_pct > 5:
        print("✅ Moderate performance (5-15% annual)")
    elif results.total_return_pct > 0:
        print("⚠️  Weak performance (0-5% annual)")
    else:
        print("❌ Strategy lost money")
    
    print()
    
    if results.win_rate >= 55:
        print("✅ Good win rate (≥55%)")
    elif results.win_rate >= 50:
        print("⚠️  Marginal win rate (50-55%)")
    else:
        print("❌ Poor win rate (<50%)")
    
    print()
    
    if results.sharpe_ratio > 1.5:
        print("✅ Excellent risk-adjusted returns (Sharpe >1.5)")
    elif results.sharpe_ratio > 1.0:
        print("✅ Good risk-adjusted returns (Sharpe 1.0-1.5)")
    elif results.sharpe_ratio > 0:
        print("⚠️  Weak risk-adjusted returns (Sharpe <1.0)")
    else:
        print("❌ Negative risk-adjusted returns")
    
    print()
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Backtest spread signals")
    parser.add_argument(
        "--symbol",
        type=str,
        default="NIFTY",
        help="Symbol to backtest (default: NIFTY)"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        required=True,
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end-date",
        type=str,
        required=True,
        help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--hold-days",
        type=int,
        default=3,
        help="Days to hold each position (default: 3)"
    )
    
    args = parser.parse_args()
    
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
    
    storage = ChainStorage()
    
    results = run_backtest(
        args.symbol,
        start_date,
        end_date,
        storage,
        args.hold_days
    )
    
    print_results(results)


if __name__ == "__main__":
    main()
