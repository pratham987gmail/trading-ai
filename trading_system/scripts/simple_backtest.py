#!/usr/bin/env python3
"""
Simple Backtest - Test basic spread strategy on historical data

This is a simplified version that doesn't require the full signal engine.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta, date
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage
from analysis.signal_models import OptionChain


def find_spread_opportunities(chain: OptionChain, spot_price: float):
    """
    Simple spread finder - bull call spreads only.
    
    Rules:
    - Long strike: ATM or 1-2% OTM
    - Short strike: 2-3% higher
    - Max net debit: Rs 20 per unit (after ₹125 costs = ₹1125 total)
    """
    opportunities = []
    
    strikes = sorted([s for s in chain.strikes if s.call_ltp], key=lambda x: x.strike)
    
    for i, long_strike_data in enumerate(strikes):
        long_strike = long_strike_data.strike
        
        # Only consider strikes near ATM (95% to 103% of spot)
        if not (spot_price * 0.95 <= long_strike <= spot_price * 1.03):
            continue
        
        # Need liquid options
        if not long_strike_data.call_ltp or long_strike_data.call_oi < 50000:
            continue
        
        # Find short strike (2-4 strikes higher)
        for j in range(i+2, min(i+5, len(strikes))):
            short_strike_data = strikes[j]
            short_strike = short_strike_data.strike
            
            if not short_strike_data.call_ltp or short_strike_data.call_oi < 30000:
                continue
            
            # Calculate spread
            # Conservative: buy at ask, sell at bid (if available)
            long_price = long_strike_data.call_ask if long_strike_data.call_ask else long_strike_data.call_ltp
            short_price = short_strike_data.call_bid if short_strike_data.call_bid else short_strike_data.call_ltp
            
            if not long_price or not short_price:
                continue
            
            net_debit = long_price - short_price
            
            # Skip if too expensive or too cheap
            if net_debit <= 5 or net_debit > 20:
                continue
            
            lot_size = 50  # NIFTY lot size
            gross_cost = net_debit * lot_size
            costs = 125  # Approximate transaction costs
            total_cost = gross_cost + costs
            
            # Budget check: max loss Rs 1000
            if total_cost > 1000:
                continue
            
            width = short_strike - long_strike
            max_profit = (width - net_debit) * lot_size
            
            if max_profit <= 0:
                continue
            
            rr = max_profit / total_cost
            
            # Need at least 1:1 risk-reward
            if rr < 1.0:
                continue
            
            opportunities.append({
                'long_strike': long_strike,
                'short_strike': short_strike,
                'long_price': long_price,
                'short_price': short_price,
                'net_debit': net_debit,
                'total_cost': total_cost,
                'max_profit': max_profit,
                'risk_reward': rr,
                'long_oi': long_strike_data.call_oi,
                'short_oi': short_strike_data.call_oi
            })
    
    # Return best by risk-reward
    if opportunities:
        return sorted(opportunities, key=lambda x: x['risk_reward'], reverse=True)[0]
    return None


def simulate_exit(entry_spread, exit_chain: OptionChain):
    """Simulate closing the spread at exit"""
    if not exit_chain:
        return 0  # Assume worthless at expiry
    
    # Find matching strikes
    long_exit_price = None
    short_exit_price = None
    
    for strike_data in exit_chain.strikes:
        if strike_data.strike == entry_spread['long_strike']:
            # Sell long position at bid
            long_exit_price = strike_data.call_bid if strike_data.call_bid else strike_data.call_ltp
        if strike_data.strike == entry_spread['short_strike']:
            # Buy back short position at ask
            short_exit_price = strike_data.call_ask if strike_data.call_ask else strike_data.call_ltp
    
    if not long_exit_price:
        long_exit_price = 0  # Worthless
    if not short_exit_price:
        short_exit_price = 0
    
    # Exit value
    lot_size = 50
    net_credit = long_exit_price - short_exit_price
    exit_value = net_credit * lot_size
    
    # Subtract exit costs
    exit_costs = 125
    return max(exit_value - exit_costs, 0)


def run_backtest(symbol: str, start_date: date, end_date: date, hold_days: int):
    """Run simple backtest"""
    
    print(f"\n{'=' * 70}")
    print(f"SIMPLE BACKTEST - Bull Call Spreads")
    print(f"{'=' * 70}")
    print(f"Symbol: {symbol}")
    print(f"Period: {start_date} to {end_date}")
    print(f"Hold: {hold_days} days")
    print(f"Max Loss: Rs 1,000 per trade")
    print()
    
    storage = ChainStorage()
    
    # Check data availability
    first_date, last_date = storage.get_date_range(symbol)
    if not first_date:
        print(f"[ERROR] No data found for {symbol}")
        return
    
    print(f"Data available: {first_date} to {last_date}")
    print()
    
    trades = []
    current_date = start_date
    
    print("Running backtest...")
    print("=" * 70)
    
    while current_date <= end_date:
        if current_date.weekday() >= 5:  # Skip weekends
            current_date += timedelta(days=1)
            continue
        
        # Get chain for entry
        entry_chains = storage.get_chains_on_date(symbol, current_date)
        
        if not entry_chains:
            current_date += timedelta(days=1)
            continue
        
        entry_chain = entry_chains[0]  # Use first chain (latest in day)
        
        # Find opportunity
        spread = find_spread_opportunities(entry_chain, entry_chain.spot_price)
        
        if not spread:
            print(f"[{current_date}] No valid spreads found")
            current_date += timedelta(days=1)
            continue
        
        # Calculate exit date
        exit_date = current_date + timedelta(days=hold_days)
        
        # Don't hold past expiry-1
        expiry_exit = entry_chain.expiry - timedelta(days=1)
        if exit_date > expiry_exit:
            exit_date = expiry_exit
        
        # Get exit chain
        exit_chains = storage.get_chains_on_date(symbol, exit_date)
        
        if not exit_chains:
            print(f"[{current_date}] No exit data for {exit_date}")
            current_date += timedelta(days=1)
            continue
        
        exit_chain = exit_chains[0]
        
        # Simulate exit
        exit_value = simulate_exit(spread, exit_chain)
        entry_cost = spread['total_cost']
        pnl = exit_value - entry_cost
        return_pct = (pnl / entry_cost) * 100 if entry_cost > 0 else 0
        
        status = "[WIN]" if pnl > 0 else "[LOSS]"
        print(f"{status} {current_date} -> {exit_date}: Rs {pnl:+,.0f} ({return_pct:+.1f}%)")
        print(f"     {spread['long_strike']} / {spread['short_strike']} CE spread @ Rs {spread['net_debit']:.2f}")
        
        trades.append({
            'entry_date': current_date,
            'exit_date': exit_date,
            'entry_cost': entry_cost,
            'exit_value': exit_value,
            'pnl': pnl,
            'return_pct': return_pct,
            'spread': spread
        })
        
        current_date += timedelta(days=1)
    
    # Results
    print()
    print("=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    
    if not trades:
        print("\n[ERROR] No trades executed")
        return
    
    winning_trades = [t for t in trades if t['pnl'] > 0]
    losing_trades = [t for t in trades if t['pnl'] <= 0]
    
    total_pnl = sum(t['pnl'] for t in trades)
    win_rate = (len(winning_trades) / len(trades)) * 100
    
    avg_win = sum(t['pnl'] for t in winning_trades) / len(winning_trades) if winning_trades else 0
    avg_loss = sum(t['pnl'] for t in losing_trades) / len(losing_trades) if losing_trades else 0
    
    capital = 100000
    total_return = (total_pnl / capital) * 100
    
    print(f"\nTotal Trades:      {len(trades)}")
    print(f"Winning Trades:    {len(winning_trades)} ({win_rate:.1f}%)")
    print(f"Losing Trades:     {len(losing_trades)}")
    print()
    print(f"Average Win:       Rs {avg_win:,.0f}")
    print(f"Average Loss:      Rs {avg_loss:,.0f}")
    if avg_loss != 0:
        print(f"Win/Loss Ratio:    {abs(avg_win / avg_loss):.2f}")
    print()
    print(f"Total P&L:         Rs {total_pnl:,.0f}")
    print(f"Total Return:      {total_return:+.2f}% (on Rs 1,00,000 capital)")
    print()
    
    # Calculate Sharpe (simplified)
    returns = [t['return_pct'] for t in trades]
    avg_return = sum(returns) / len(returns)
    std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
    sharpe = (avg_return / std_return) if std_return > 0 else 0
    
    print(f"Sharpe Ratio:      {sharpe:.2f}")
    
    # Interpretation
    print()
    print("=" * 70)
    print("INTERPRETATION")
    print("=" * 70)
    
    if total_return > 15:
        print("\n[EXCELLENT] Strong performance (>15% return)")
        print("-> Proceed to paper trading")
    elif total_return > 5:
        print("\n[GOOD] Moderate performance (5-15% return)")
        print("-> Worth paper trading to validate")
    elif total_return > 0:
        print("\n[WEAK] Marginal performance (0-5% return)")
        print("-> Try different parameters or strategies")
    else:
        print("\n[FAIL] Strategy lost money")
        print("-> Redesign or abandon")
    
    print()
    
    if win_rate >= 55:
        print("[OK] Good win rate (>=55%)")
    elif win_rate >= 50:
        print("[MARGINAL] Borderline win rate (50-55%)")
    else:
        print("[POOR] Low win rate (<50%)")
    
    print()
    
    if sharpe > 1.5:
        print("[EXCELLENT] Strong risk-adjusted returns (Sharpe >1.5)")
    elif sharpe > 1.0:
        print("[GOOD] Decent risk-adjusted returns (Sharpe 1.0-1.5)")
    elif sharpe > 0:
        print("[WEAK] Poor risk-adjusted returns (Sharpe <1.0)")
    else:
        print("[FAIL] Negative risk-adjusted returns")
    
    print()
    print("=" * 70)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Simple spread backtest")
    parser.add_argument("--symbol", default="NIFTY", help="Symbol (default: NIFTY)")
    parser.add_argument("--start-date", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--hold-days", type=int, default=3, help="Days to hold (default: 3)")
    
    args = parser.parse_args()
    
    start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    
    run_backtest(args.symbol, start, end, args.hold_days)
