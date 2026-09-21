#!/usr/bin/env python3
"""
Compare all backtest results and show summary
"""

from pathlib import Path
import re

results_dir = Path(__file__).parent.parent / "backtest_results"

if not results_dir.exists():
    print("No results found yet!")
    print("Run TEST_ALL_AND_SAVE.bat first")
    exit()

results = []

for file in results_dir.glob("backtest_*.txt"):
    with open(file, 'r') as f:
        content = f.read()
        
        # Extract key metrics
        symbol = re.search(r'Symbol: (\w+)', content)
        hold_days = re.search(r'Hold Days: (\d+)', content)
        total_return = re.search(r'Total Return:\s+([\+\-]?\d+\.?\d*)%', content)
        win_rate = re.search(r'Winning Trades:.*\((\d+\.?\d*)%\)', content)
        total_trades = re.search(r'Total Trades:\s+(\d+)', content)
        sharpe = re.search(r'Sharpe Ratio:\s+([\-]?\d+\.?\d*)', content)
        
        if all([symbol, hold_days, total_return, win_rate, total_trades]):
            results.append({
                'file': file.name,
                'symbol': symbol.group(1),
                'hold_days': int(hold_days.group(1)),
                'return': float(total_return.group(1)),
                'win_rate': float(win_rate.group(1)),
                'trades': int(total_trades.group(1)),
                'sharpe': float(sharpe.group(1)) if sharpe else 0.0
            })

if not results:
    print("No completed backtest results found!")
    exit()

# Sort by return (best first)
results.sort(key=lambda x: x['return'], reverse=True)

print("\n" + "=" * 80)
print("BACKTEST RESULTS SUMMARY")
print("=" * 80)
print()

print(f"{'Rank':<6} {'Symbol':<12} {'Hold':<6} {'Return':<10} {'Win Rate':<10} {'Trades':<8} {'Sharpe':<8}")
print("-" * 80)

for i, r in enumerate(results, 1):
    status = ""
    if r['return'] > 15:
        status = " *** EXCELLENT ***"
    elif r['return'] > 10:
        status = " ** GOOD **"
    elif r['return'] > 5:
        status = " * OK *"
    elif r['return'] > 0:
        status = " (weak)"
    else:
        status = " FAIL"
    
    print(f"{i:<6} {r['symbol']:<12} {r['hold_days']}d     {r['return']:+6.2f}%    {r['win_rate']:5.1f}%     {r['trades']:<8} {r['sharpe']:6.2f} {status}")

print()
print("=" * 80)
print("RECOMMENDATION")
print("=" * 80)

best = results[0]

if best['return'] > 15:
    print("\n[EXCELLENT] Best strategy shows >15% return")
    print(f"  -> {best['symbol']} with {best['hold_days']}-day hold: {best['return']:+.2f}%")
    print("  -> Worth paper trading to validate")
elif best['return'] > 10:
    print("\n[GOOD] Best strategy shows 10-15% return")
    print(f"  -> {best['symbol']} with {best['hold_days']}-day hold: {best['return']:+.2f}%")
    print("  -> Consider paper trading")
elif best['return'] > 5:
    print("\n[MARGINAL] Best strategy shows 5-10% return")
    print(f"  -> {best['symbol']} with {best['hold_days']}-day hold: {best['return']:+.2f}%")
    print("  -> Borderline, maybe try more parameters")
else:
    print("\n[FAIL] No strategy shows >5% return")
    print(f"  -> Best was {best['symbol']} {best['hold_days']}d: {best['return']:+.2f}%")
    print("  -> Strategy doesn't work - save your capital!")

print()
print("To see full details of any result:")
print(f"  notepad backtest_results\\{best['file']}")
print()
