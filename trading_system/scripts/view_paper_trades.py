"""
View Paper Trading Results

Shows all paper trades and statistics.
"""

import json
from pathlib import Path
from datetime import datetime


def view_trades():
    """View all paper trades."""
    
    trades_file = Path("paper_trades.json")
    
    if not trades_file.exists():
        print("\n❌ No paper trades found!")
        print("\nStart paper trading first:")
        print("  Double-click: START_PAPER_TRADING.bat")
        return
    
    with open(trades_file, 'r') as f:
        data = json.load(f)
    
    trades = data.get('trades', [])
    
    if not trades:
        print("\n📊 No trades yet - system is monitoring for signals")
        return
    
    print("\n" + "="*80)
    print("📊 PAPER TRADING JOURNAL")
    print("="*80 + "\n")
    
    closed_trades = [t for t in trades if t['status'] == 'CLOSED']
    open_trades = [t for t in trades if t['status'] == 'OPEN']
    
    # Show open trades
    if open_trades:
        print("📌 OPEN POSITIONS:")
        for t in open_trades:
            entry = datetime.fromisoformat(t['entry_date'])
            days_held = (datetime.now() - entry).days
            cost = t['entry_premium'] * t['quantity']
            
            print(f"\n   Trade #{t['trade_id']}: Long Put {t['entry_strike']}")
            print(f"   Entry: {entry.strftime('%Y-%m-%d %H:%M')}")
            print(f"   Spot at Entry: ₹{t['entry_spot']:,.2f}")
            print(f"   Premium: ₹{t['entry_premium']:.2f}")
            print(f"   Cost: ₹{cost:,.2f}")
            print(f"   Days Held: {days_held}")
        print()
    
    # Show closed trades
    if closed_trades:
        print(f"📜 CLOSED TRADES ({len(closed_trades)} total):\n")
        
        for t in closed_trades:
            entry = datetime.fromisoformat(t['entry_date'])
            exit_dt = datetime.fromisoformat(t['exit_date'])
            
            status = "✅ WIN" if t['pnl'] > 0 else "❌ LOSS"
            
            print(f"{status} | Trade #{t['trade_id']} | {t['entry_strike']} PE")
            print(f"   Entry: {entry.strftime('%Y-%m-%d %H:%M')} @ ₹{t['entry_spot']:,.2f}")
            print(f"   Exit:  {exit_dt.strftime('%Y-%m-%d %H:%M')} @ ₹{t['exit_spot']:,.2f}")
            print(f"   Hold: {t['hold_days']} days | P&L: ₹{t['pnl']:+,.2f} ({t['return_pct']:+.2f}%)")
            print()
        
        # Statistics
        winners = [t for t in closed_trades if t['pnl'] > 0]
        losers = [t for t in closed_trades if t['pnl'] <= 0]
        
        total_pnl = sum(t['pnl'] for t in closed_trades)
        win_rate = (len(winners) / len(closed_trades)) * 100
        
        avg_win = sum(t['pnl'] for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t['pnl'] for t in losers) / len(losers) if losers else 0
        
        print("="*80)
        print("📊 STATISTICS")
        print("="*80)
        print(f"Total Trades:    {len(closed_trades)}")
        print(f"Winners:         {len(winners)} ({win_rate:.1f}%)")
        print(f"Losers:          {len(losers)}")
        print()
        print(f"Avg Win:         ₹{avg_win:,.2f}")
        print(f"Avg Loss:        ₹{avg_loss:,.2f}")
        print()
        print(f"Total P&L:       ₹{total_pnl:+,.2f}")
        print(f"Return:          {(total_pnl / 100000) * 100:+.2f}%")
        print()
        
        # Compare to backtest
        print("🎯 BACKTEST COMPARISON:")
        print(f"   Backtest Win Rate: 43.1%")
        print(f"   Your Paper:        {win_rate:.1f}%")
        print()
        print(f"   Backtest Avg/Month: ₹11,861")
        if len(closed_trades) >= 4:  # At least 4 trades for monthly estimate
            weeks = max(1, (datetime.fromisoformat(closed_trades[-1]['exit_date']) - 
                           datetime.fromisoformat(closed_trades[0]['entry_date'])).days / 7)
            estimated_monthly = (total_pnl / weeks) * 4 if weeks > 0 else 0
            print(f"   Your Paper:         ₹{estimated_monthly:,.2f}")
        
        print("="*80)
    
    print(f"\nLast Updated: {data.get('last_updated', 'Unknown')}")
    print()


if __name__ == "__main__":
    view_trades()
