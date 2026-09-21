"""
COMPREHENSIVE STRATEGY TEST ACROSS ALL DATA

Tests all strategies on MULTIPLE time periods to find:
1. Which strategy works best overall
2. Which strategy works in different market conditions
3. Most consistent performer across all periods
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from compare_all_strategies import (
    backtest_strategy, calculate_stats,
    build_iron_condor, build_bull_call_spread, build_bear_put_spread,
    build_long_call, build_long_put, build_short_straddle
)

sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage


def split_into_periods(start_date, end_date, period_months=1):
    """Split date range into monthly periods."""
    periods = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    
    while current < end:
        period_start = current
        # Move to next month
        next_month = current.month + period_months
        next_year = current.year
        while next_month > 12:
            next_month -= 12
            next_year += 1
        
        period_end = datetime(next_year, next_month, 1) - timedelta(days=1)
        if period_end > end:
            period_end = end
        
        # Only include if we have at least 2 weeks of data
        if (period_end - period_start).days >= 14:
            periods.append({
                "start": period_start.strftime("%Y-%m-%d"),
                "end": period_end.strftime("%Y-%m-%d"),
                "label": period_start.strftime("%b %Y")
            })
        
        current = period_end + timedelta(days=1)
    
    return periods


def run_comprehensive_test(symbol="NIFTY", hold_days=7):
    """Test all strategies across all time periods."""
    print("\n" + "="*80)
    print("🌍 COMPREHENSIVE STRATEGY TEST - ALL DATA")
    print("="*80)
    print(f"Symbol: {symbol}")
    print(f"Hold Period: {hold_days} days")
    print("="*80 + "\n")
    
    storage = ChainStorage()
    date_range = storage.get_date_range(symbol)
    
    if not date_range or not date_range[0]:
        print("❌ No data available!")
        return
    
    start_date, end_date = date_range
    print(f"📅 Total Data Range: {start_date} to {end_date}\n")
    
    # Split into monthly periods
    periods = split_into_periods(start_date, end_date, period_months=1)
    print(f"📊 Testing {len(periods)} periods:\n")
    
    strategies = [
        ("Iron Condor", lambda c: build_iron_condor(c, 100, 100)),
        ("Bull Call Spread", lambda c: build_bull_call_spread(c, 0, 100)),
        ("Bear Put Spread", lambda c: build_bear_put_spread(c, 0, 100)),
        ("Long Call", lambda c: build_long_call(c, 0)),
        ("Long Put", lambda c: build_long_put(c, 0)),
        ("Short Straddle", build_short_straddle),
    ]
    
    # Store results by strategy and period
    all_results = {name: [] for name, _ in strategies}
    
    # Test each period
    for i, period in enumerate(periods, 1):
        print(f"Testing Period {i}/{len(periods)}: {period['label']} ({period['start']} to {period['end']})")
        
        for name, builder in strategies:
            trades = backtest_strategy(
                storage, symbol, 
                period['start'], period['end'], 
                builder, hold_days
            )
            
            if trades:
                stats = calculate_stats(trades, name)
                if stats:
                    stats['period'] = period['label']
                    stats['start'] = period['start']
                    stats['end'] = period['end']
                    all_results[name].append(stats)
        
        print()
    
    # Calculate aggregate statistics
    print("\n" + "="*80)
    print("📊 OVERALL PERFORMANCE (All Periods Combined)")
    print("="*80 + "\n")
    
    strategy_aggregates = []
    
    for name, _ in strategies:
        results = all_results[name]
        
        if not results:
            continue
        
        total_trades = sum(r['total_trades'] for r in results)
        total_wins = sum(r['win_count'] for r in results)
        total_pnl = sum(r['total_pnl'] for r in results)
        
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        avg_pnl_per_period = total_pnl / len(results) if results else 0
        
        # Count profitable periods
        profitable_periods = sum(1 for r in results if r['total_pnl'] > 0)
        consistency = (profitable_periods / len(results) * 100) if results else 0
        
        # Average Sharpe
        avg_sharpe = sum(r['sharpe'] for r in results) / len(results) if results else 0
        
        strategy_aggregates.append({
            'strategy': name,
            'total_pnl': total_pnl,
            'total_trades': total_trades,
            'win_rate': win_rate,
            'consistency': consistency,
            'avg_sharpe': avg_sharpe,
            'periods_tested': len(results),
            'profitable_periods': profitable_periods,
        })
    
    # Sort by total P&L
    strategy_aggregates.sort(key=lambda x: x['total_pnl'], reverse=True)
    
    # Display results
    for i, agg in enumerate(strategy_aggregates, 1):
        rank = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        
        print(f"{rank} {agg['strategy']}")
        print(f"   Total P&L:           ₹{agg['total_pnl']:,.0f}")
        print(f"   Total Trades:        {agg['total_trades']}")
        print(f"   Overall Win Rate:    {agg['win_rate']:.1f}%")
        print(f"   Consistency:         {agg['consistency']:.1f}% ({agg['profitable_periods']}/{agg['periods_tested']} periods profitable)")
        print(f"   Avg Sharpe:          {agg['avg_sharpe']:.2f}")
        print()
    
    # Show winner
    if strategy_aggregates:
        winner = strategy_aggregates[0]
        print("="*80)
        print(f"🏆 OVERALL WINNER: {winner['strategy']}")
        print("="*80)
        print(f"Total Profit:        ₹{winner['total_pnl']:,.0f}")
        print(f"Consistency:         {winner['consistency']:.1f}% of periods profitable")
        print(f"Win Rate:            {winner['win_rate']:.1f}%")
        print(f"Avg Sharpe:          {winner['avg_sharpe']:.2f}")
        print()
        
        if winner['total_pnl'] > 50000:
            print("✅ HIGHLY PROFITABLE - Strong edge across all market conditions!")
        elif winner['total_pnl'] > 20000:
            print("✅ PROFITABLE - Good strategy worth implementing")
        elif winner['total_pnl'] > 0:
            print("⚠️  MARGINALLY PROFITABLE - Requires careful selection of market conditions")
        else:
            print("❌ NOT PROFITABLE - No consistent edge found")
        
        print("="*80)
    
    # Show period-by-period breakdown for winner
    if strategy_aggregates:
        winner_name = strategy_aggregates[0]['strategy']
        winner_results = all_results[winner_name]
        
        print(f"\n📅 PERIOD-BY-PERIOD BREAKDOWN: {winner_name}")
        print("="*80 + "\n")
        
        for r in winner_results:
            status = "✅" if r['total_pnl'] > 0 else "❌"
            print(f"{status} {r['period']:8} | P&L: ₹{r['total_pnl']:>8,.0f} | Win Rate: {r['win_rate']:>5.1f}% | Sharpe: {r['sharpe']:>5.2f}")
        
        print()
    
    # Show best strategy per period
    print("\n🎯 BEST STRATEGY BY PERIOD")
    print("="*80 + "\n")
    
    for period in periods:
        period_label = period['label']
        
        # Find best strategy for this period
        best_pnl = -999999
        best_strategy = None
        
        for name, results in all_results.items():
            for r in results:
                if r['period'] == period_label and r['total_pnl'] > best_pnl:
                    best_pnl = r['total_pnl']
                    best_strategy = name
        
        if best_strategy:
            status = "🟢" if best_pnl > 1000 else "🟡" if best_pnl > 0 else "🔴"
            print(f"{status} {period_label:8} | Best: {best_strategy:20} | P&L: ₹{best_pnl:>8,.0f}")
    
    print("\n" + "="*80)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Test All Strategies Across All Data")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--hold-days", type=int, default=7)
    
    args = parser.parse_args()
    
    run_comprehensive_test(args.symbol, args.hold_days)
