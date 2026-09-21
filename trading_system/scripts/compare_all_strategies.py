"""
ULTIMATE STRATEGY COMPARISON

Tests ALL major options strategies on the same data to find the most profitable one.

Strategies Tested:
1. Iron Condor (sideways/premium selling)
2. Bull Call Spread (bullish directional)
3. Bear Put Spread (bearish directional)
4. Long Call (strong bullish)
5. Long Put (strong bearish)
6. Short Straddle (high premium, high risk)
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage


def find_atm_strike(strikes, spot):
    """Find ATM strike."""
    return min(strikes, key=lambda s: abs(s.strike - spot))


def build_iron_condor(chain, otm=100, wing=100):
    """Build Iron Condor: Sell OTM call/put spread."""
    spot = chain.spot_price
    atm = find_atm_strike(chain.strikes, spot).strike
    
    strikes_dict = {s.strike: s for s in chain.strikes}
    
    sell_call_strike = atm + otm
    buy_call_strike = atm + otm + wing
    sell_put_strike = atm - otm
    buy_put_strike = atm - otm - wing
    
    # Check all strikes exist
    if not all(k in strikes_dict for k in [sell_call_strike, buy_call_strike, sell_put_strike, buy_put_strike]):
        return None
    
    # Get prices
    sc = strikes_dict[sell_call_strike]
    bc = strikes_dict[buy_call_strike]
    sp = strikes_dict[sell_put_strike]
    bp = strikes_dict[buy_put_strike]
    
    if not all([sc.call_ltp, bc.call_ltp, sp.put_ltp, bp.put_ltp]):
        return None
    
    premium = (sc.call_ltp + sp.put_ltp) - (bc.call_ltp + bp.put_ltp)
    if premium <= 0:
        return None
    
    return {
        "type": "IRON_CONDOR",
        "spot": spot,
        "premium": premium * 50,  # NIFTY lot size
        "max_loss": (wing - premium) * 50,
        "strikes": [buy_call_strike, sell_call_strike, sell_put_strike, buy_put_strike],
    }


def build_bull_call_spread(chain, otm=0, width=100):
    """Build Bull Call Spread: Buy ATM call, sell OTM call."""
    spot = chain.spot_price
    atm = find_atm_strike(chain.strikes, spot).strike
    
    strikes_dict = {s.strike: s for s in chain.strikes}
    
    buy_strike = atm + otm
    sell_strike = atm + otm + width
    
    if buy_strike not in strikes_dict or sell_strike not in strikes_dict:
        return None
    
    buy_leg = strikes_dict[buy_strike]
    sell_leg = strikes_dict[sell_strike]
    
    if not buy_leg.call_ltp or not sell_leg.call_ltp:
        return None
    
    debit = buy_leg.call_ltp - sell_leg.call_ltp
    if debit <= 0:
        return None
    
    return {
        "type": "BULL_CALL_SPREAD",
        "spot": spot,
        "debit": debit * 50,
        "max_profit": (width - debit) * 50,
        "max_loss": debit * 50,
        "strikes": [buy_strike, sell_strike],
    }


def build_bear_put_spread(chain, otm=0, width=100):
    """Build Bear Put Spread: Buy ATM put, sell OTM put."""
    spot = chain.spot_price
    atm = find_atm_strike(chain.strikes, spot).strike
    
    strikes_dict = {s.strike: s for s in chain.strikes}
    
    buy_strike = atm - otm
    sell_strike = atm - otm - width
    
    if buy_strike not in strikes_dict or sell_strike not in strikes_dict:
        return None
    
    buy_leg = strikes_dict[buy_strike]
    sell_leg = strikes_dict[sell_strike]
    
    if not buy_leg.put_ltp or not sell_leg.put_ltp:
        return None
    
    debit = buy_leg.put_ltp - sell_leg.put_ltp
    if debit <= 0:
        return None
    
    return {
        "type": "BEAR_PUT_SPREAD",
        "spot": spot,
        "debit": debit * 50,
        "max_profit": (width - debit) * 50,
        "max_loss": debit * 50,
        "strikes": [buy_strike, sell_strike],
    }


def build_long_call(chain, otm=0):
    """Build Long Call: Buy ATM/OTM call."""
    spot = chain.spot_price
    atm = find_atm_strike(chain.strikes, spot).strike
    
    strikes_dict = {s.strike: s for s in chain.strikes}
    
    buy_strike = atm + otm
    
    if buy_strike not in strikes_dict:
        return None
    
    buy_leg = strikes_dict[buy_strike]
    
    if not buy_leg.call_ltp:
        return None
    
    return {
        "type": "LONG_CALL",
        "spot": spot,
        "debit": buy_leg.call_ltp * 50,
        "max_loss": buy_leg.call_ltp * 50,
        "strikes": [buy_strike],
    }


def build_long_put(chain, otm=0):
    """Build Long Put: Buy ATM/OTM put."""
    spot = chain.spot_price
    atm = find_atm_strike(chain.strikes, spot).strike
    
    strikes_dict = {s.strike: s for s in chain.strikes}
    
    buy_strike = atm - otm
    
    if buy_strike not in strikes_dict:
        return None
    
    buy_leg = strikes_dict[buy_strike]
    
    if not buy_leg.put_ltp:
        return None
    
    return {
        "type": "LONG_PUT",
        "spot": spot,
        "debit": buy_leg.put_ltp * 50,
        "max_loss": buy_leg.put_ltp * 50,
        "strikes": [buy_strike],
    }


def build_short_straddle(chain):
    """Build Short Straddle: Sell ATM call + put."""
    spot = chain.spot_price
    atm_strike_obj = find_atm_strike(chain.strikes, spot)
    atm = atm_strike_obj.strike
    
    if not atm_strike_obj.call_ltp or not atm_strike_obj.put_ltp:
        return None
    
    premium = (atm_strike_obj.call_ltp + atm_strike_obj.put_ltp) * 50
    
    return {
        "type": "SHORT_STRADDLE",
        "spot": spot,
        "premium": premium,
        "max_loss": 999999,  # Unlimited risk
        "strikes": [atm],
    }


def calculate_pnl(strategy, entry_spot, exit_spot):
    """Calculate P&L for any strategy."""
    strategy_type = strategy["type"]
    
    if strategy_type == "IRON_CONDOR":
        strikes = strategy["strikes"]
        buy_call, sell_call, sell_put, buy_put = strikes
        
        pnl = strategy["premium"]
        
        # Call side
        if exit_spot > sell_call:
            pnl -= (exit_spot - sell_call) * 50
            if exit_spot > buy_call:
                pnl += (exit_spot - buy_call) * 50
        
        # Put side
        if exit_spot < sell_put:
            pnl -= (sell_put - exit_spot) * 50
            if exit_spot < buy_put:
                pnl += (buy_put - exit_spot) * 50
        
        return max(min(pnl, strategy["premium"]), -strategy["max_loss"])
    
    elif strategy_type == "BULL_CALL_SPREAD":
        buy_strike, sell_strike = strategy["strikes"]
        
        intrinsic = 0
        if exit_spot > buy_strike:
            intrinsic = min(exit_spot - buy_strike, sell_strike - buy_strike) * 50
        
        return intrinsic - strategy["debit"]
    
    elif strategy_type == "BEAR_PUT_SPREAD":
        buy_strike, sell_strike = strategy["strikes"]
        
        intrinsic = 0
        if exit_spot < buy_strike:
            intrinsic = min(buy_strike - exit_spot, buy_strike - sell_strike) * 50
        
        return intrinsic - strategy["debit"]
    
    elif strategy_type == "LONG_CALL":
        buy_strike = strategy["strikes"][0]
        
        intrinsic = max(0, exit_spot - buy_strike) * 50
        return intrinsic - strategy["debit"]
    
    elif strategy_type == "LONG_PUT":
        buy_strike = strategy["strikes"][0]
        
        intrinsic = max(0, buy_strike - exit_spot) * 50
        return intrinsic - strategy["debit"]
    
    elif strategy_type == "SHORT_STRADDLE":
        atm = strategy["strikes"][0]
        
        pnl = strategy["premium"]
        
        if exit_spot > atm:
            pnl -= (exit_spot - atm) * 50
        elif exit_spot < atm:
            pnl -= (atm - exit_spot) * 50
        
        return pnl
    
    return 0


def backtest_strategy(storage, symbol, start_date, end_date, strategy_builder, hold_days=7):
    """Backtest a single strategy."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    trades = []
    current_date = start_dt
    
    while current_date <= end_dt:
        chains = storage.get_chains_on_date(symbol, current_date.date())
        
        if not chains:
            current_date += timedelta(days=1)
            continue
        
        entry_chain = chains[0]
        strategy = strategy_builder(entry_chain)
        
        if not strategy:
            current_date += timedelta(days=1)
            continue
        
        exit_date = current_date + timedelta(days=hold_days)
        if exit_date > end_dt:
            break
        
        exit_chains = storage.get_chains_on_date(symbol, exit_date.date())
        
        if not exit_chains:
            current_date += timedelta(days=1)
            continue
        
        exit_spot = exit_chains[0].spot_price
        entry_spot = strategy["spot"]
        
        pnl = calculate_pnl(strategy, entry_spot, exit_spot)
        
        trades.append({
            "entry_date": current_date.strftime("%Y-%m-%d"),
            "exit_date": exit_date.strftime("%Y-%m-%d"),
            "entry_spot": entry_spot,
            "exit_spot": exit_spot,
            "pnl": pnl,
            "winner": pnl > 0,
        })
        
        current_date += timedelta(days=hold_days + 1)
    
    return trades


def calculate_stats(trades, strategy_name):
    """Calculate performance statistics."""
    if not trades:
        return None
    
    total = len(trades)
    winners = [t for t in trades if t["winner"]]
    losers = [t for t in trades if not t["winner"]]
    
    win_count = len(winners)
    win_rate = (win_count / total) * 100
    
    avg_win = sum(t["pnl"] for t in winners) / win_count if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0
    
    total_pnl = sum(t["pnl"] for t in trades)
    
    returns = [t["pnl"] for t in trades]
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
    std_dev = variance ** 0.5
    sharpe = (mean_return / std_dev) if std_dev > 0 else 0
    
    profit_factor = abs(sum(t["pnl"] for t in winners) / sum(t["pnl"] for t in losers)) if losers else 999
    
    return {
        "strategy": strategy_name,
        "total_trades": total,
        "win_count": win_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "total_pnl": total_pnl,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
    }


def run_comparison(symbol="NIFTY", start_date="2024-09-23", end_date="2024-10-31", hold_days=7):
    """Run all strategies and compare."""
    print("\n" + "="*80)
    print("🏆 ULTIMATE STRATEGY COMPARISON")
    print("="*80)
    print(f"Symbol: {symbol}")
    print(f"Period: {start_date} to {end_date}")
    print(f"Hold: {hold_days} days")
    print("="*80 + "\n")
    
    storage = ChainStorage()
    
    strategies = [
        ("Iron Condor", lambda c: build_iron_condor(c, 100, 100)),
        ("Bull Call Spread", lambda c: build_bull_call_spread(c, 0, 100)),
        ("Bear Put Spread", lambda c: build_bear_put_spread(c, 0, 100)),
        ("Long Call (ATM)", lambda c: build_long_call(c, 0)),
        ("Long Put (ATM)", lambda c: build_long_put(c, 0)),
        ("Short Straddle", build_short_straddle),
    ]
    
    results = []
    
    for name, builder in strategies:
        print(f"Testing {name}...")
        trades = backtest_strategy(storage, symbol, start_date, end_date, builder, hold_days)
        stats = calculate_stats(trades, name)
        if stats:
            results.append(stats)
    
    # Sort by total P&L
    results.sort(key=lambda x: x["total_pnl"], reverse=True)
    
    print("\n" + "="*80)
    print("📊 RESULTS (Ranked by Total P&L)")
    print("="*80 + "\n")
    
    for i, r in enumerate(results, 1):
        rank_emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        
        print(f"{rank_emoji} {r['strategy']}")
        print(f"   Total P&L:      ₹{r['total_pnl']:,.0f}")
        print(f"   Win Rate:       {r['win_rate']:.1f}% ({r['win_count']}/{r['total_trades']})")
        print(f"   Avg Win:        ₹{r['avg_win']:,.0f}")
        print(f"   Avg Loss:       ₹{r['avg_loss']:,.0f}")
        print(f"   Sharpe:         {r['sharpe']:.2f}")
        print(f"   Profit Factor:  {r['profit_factor']:.2f}")
        print()
    
    # Show winner
    if results:
        winner = results[0]
        print("="*80)
        print(f"🎉 WINNER: {winner['strategy']}")
        print("="*80)
        print(f"Total Profit: ₹{winner['total_pnl']:,.0f}")
        print(f"Win Rate: {winner['win_rate']:.1f}%")
        print(f"Sharpe Ratio: {winner['sharpe']:.2f}")
        print()
        
        if winner['total_pnl'] > 5000:
            print("✅ HIGHLY PROFITABLE - This strategy has a strong edge!")
        elif winner['total_pnl'] > 2000:
            print("✅ PROFITABLE - Good strategy, worth paper trading")
        elif winner['total_pnl'] > 0:
            print("⚠️  MARGINALLY PROFITABLE - Proceed with caution")
        else:
            print("❌ NOT PROFITABLE - None of these strategies worked in this period")
        
        print("="*80)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Compare All Strategies")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--start-date", default="2024-09-23")
    parser.add_argument("--end-date", default="2024-10-31")
    parser.add_argument("--hold-days", type=int, default=7)
    
    args = parser.parse_args()
    
    run_comparison(args.symbol, args.start_date, args.end_date, args.hold_days)
