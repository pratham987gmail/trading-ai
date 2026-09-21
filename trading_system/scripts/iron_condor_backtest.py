"""
Iron Condor Backtest Script

Tests the profitability of Iron Condor (premium selling) strategy
using historical option chain data.

Iron Condor = Sell OTM Call Spread + Sell OTM Put Spread
- Max Profit: Net premium collected
- Max Loss: Wing width - premium
- Win Rate: Typically 70-80% (small consistent wins)
- Works best in sideways/low volatility markets
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
import sqlite3

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Settings
from recorder.storage import ChainStorage


def find_atm_strike(strikes, spot):
    """Find the at-the-money strike closest to spot price."""
    min_diff = float('inf')
    atm = None
    for row in strikes:
        strike = row.get("strike")
        if strike:
            diff = abs(strike - spot)
            if diff < min_diff:
                min_diff = diff
                atm = strike
    return atm


def get_leg_price(leg_data, price_mode="ltp"):
    """Get price for a leg based on pricing mode."""
    if not leg_data:
        return None
    
    bid = leg_data.get("bid")
    ask = leg_data.get("ask")
    ltp = leg_data.get("ltp")
    
    # If no bid/ask, always use LTP
    if not bid or not ask:
        return ltp
    
    if price_mode == "conservative":
        # For selling: use bid (pessimistic)
        # For buying: use ask (pessimistic)
        return bid, ask
    elif price_mode == "mid":
        if bid and ask:
            return (bid + ask) / 2
    
    return ltp


def build_iron_condor(chain, otm_offset=100, wing_width=100, lots=1):
    """
    Build an Iron Condor structure from an option chain.
    
    Args:
        chain: Option chain dict
        otm_offset: How far OTM to sell (points from ATM)
        wing_width: Width of each spread wing (points)
        lots: Number of lots
        
    Returns:
        Dict with Iron Condor details or None if can't be built
    """
    spot = chain.get("spot")
    strikes = chain.get("strikes", [])
    
    if not spot or not strikes:
        return None
    
    # Find ATM
    atm = find_atm_strike(strikes, spot)
    if not atm:
        return None
    
    # Find strikes for the structure
    # Call side: Sell ATM+offset, Buy ATM+offset+wing
    sell_call_strike = atm + otm_offset
    buy_call_strike = atm + otm_offset + wing_width
    
    # Put side: Sell ATM-offset, Buy ATM-offset-wing
    sell_put_strike = atm - otm_offset
    buy_put_strike = atm - otm_offset - wing_width
    
    # Find the leg data
    sell_call_leg = None
    buy_call_leg = None
    sell_put_leg = None
    buy_put_leg = None
    
    for row in strikes:
        strike = row.get("strike")
        if not strike:
            continue
            
        if strike == sell_call_strike:
            sell_call_leg = row.get("call")
        elif strike == buy_call_strike:
            buy_call_leg = row.get("call")
        elif strike == sell_put_strike:
            sell_put_leg = row.get("put")
        elif strike == buy_put_strike:
            buy_put_leg = row.get("put")
    
    # Check all legs exist
    if not all([sell_call_leg, buy_call_leg, sell_put_leg, buy_put_leg]):
        return None
    
    # Get prices - use LTP since no bid/ask available
    sell_call_price = get_leg_price(sell_call_leg, "ltp")
    buy_call_price = get_leg_price(buy_call_leg, "ltp")
    sell_put_price = get_leg_price(sell_put_leg, "ltp")
    buy_put_price = get_leg_price(buy_put_leg, "ltp")
    
    if None in [sell_call_price, buy_call_price, sell_put_price, buy_put_price]:
        return None
    
    # Calculate net premium (credit received)
    # Using LTP for all legs (not conservative but only data available)
    net_premium_per_unit = (sell_call_price + sell_put_price) - (buy_call_price + buy_put_price)
    
    if net_premium_per_unit <= 0:
        return None  # Should never happen for a proper IC, but check anyway
    
    # Get lot size
    lot_size = sell_call_leg.get("lot_size", 50)
    
    # Calculate payoff
    net_premium_total = net_premium_per_unit * lot_size * lots
    max_profit = net_premium_total
    
    # Max loss is the larger wing width minus premium collected
    max_loss_per_unit = wing_width - net_premium_per_unit
    max_loss_total = max_loss_per_unit * lot_size * lots
    
    # Breakevens
    breakeven_upper = sell_call_strike + net_premium_per_unit
    breakeven_lower = sell_put_strike - net_premium_per_unit
    
    return {
        "structure": "IRON_CONDOR",
        "spot": spot,
        "atm": atm,
        "sell_call_strike": sell_call_strike,
        "buy_call_strike": buy_call_strike,
        "sell_put_strike": sell_put_strike,
        "buy_put_strike": buy_put_strike,
        "net_premium_per_unit": net_premium_per_unit,
        "net_premium_total": net_premium_total,
        "max_profit": max_profit,
        "max_loss": max_loss_total,
        "risk_reward": max_profit / max_loss_total if max_loss_total > 0 else 0,
        "breakeven_upper": breakeven_upper,
        "breakeven_lower": breakeven_lower,
        "lot_size": lot_size,
        "lots": lots,
        "wing_width": wing_width,
        "otm_offset": otm_offset,
    }


def calculate_pnl_at_expiry(ic, exit_spot):
    """
    Calculate P&L at expiry based on where spot ends up.
    
    Simplified: assumes we hold to expiry and legs are valued at intrinsic.
    """
    max_profit = ic["max_profit"]
    max_loss = ic["max_loss"]
    breakeven_upper = ic["breakeven_upper"]
    breakeven_lower = ic["breakeven_lower"]
    sell_call_strike = ic["sell_call_strike"]
    sell_put_strike = ic["sell_put_strike"]
    buy_call_strike = ic["buy_call_strike"]
    buy_put_strike = ic["buy_put_strike"]
    lot_size = ic["lot_size"]
    lots = ic["lots"]
    net_premium_total = ic["net_premium_total"]
    
    # P&L calculation:
    # Start with premium collected
    pnl = net_premium_total
    
    # Calculate intrinsic value of each leg at exit
    # Calls
    if exit_spot > sell_call_strike:
        # Short call is ITM (we lose)
        short_call_loss = (exit_spot - sell_call_strike) * lot_size * lots
        pnl -= short_call_loss
        
        if exit_spot > buy_call_strike:
            # Long call is ITM (we gain, limits loss)
            long_call_gain = (exit_spot - buy_call_strike) * lot_size * lots
            pnl += long_call_gain
    
    # Puts  
    if exit_spot < sell_put_strike:
        # Short put is ITM (we lose)
        short_put_loss = (sell_put_strike - exit_spot) * lot_size * lots
        pnl -= short_put_loss
        
        if exit_spot < buy_put_strike:
            # Long put is ITM (we gain, limits loss)
            long_put_gain = (buy_put_strike - exit_spot) * lot_size * lots
            pnl += long_put_gain
    
    # Cap at max profit and max loss
    pnl = min(pnl, max_profit)
    pnl = max(pnl, -max_loss)
    
    return pnl


def run_backtest(symbol="NIFTY", start_date=None, end_date=None, hold_days=7, 
                 otm_offset=100, wing_width=100, lots=1):
    """
    Backtest Iron Condor strategy.
    
    Args:
        symbol: Underlying symbol
        start_date: Start date string (YYYY-MM-DD)
        end_date: End date string (YYYY-MM-DD)
        hold_days: How many days to hold before exit
        otm_offset: How far OTM to sell strikes (points)
        wing_width: Width of protection wings (points)
        lots: Number of lots per trade
    """
    print(f"\n{'='*70}")
    print(f"IRON CONDOR BACKTEST - {symbol}")
    print(f"{'='*70}")
    print(f"Parameters:")
    print(f"  OTM Offset: {otm_offset} points")
    print(f"  Wing Width: {wing_width} points")
    print(f"  Hold Period: {hold_days} days")
    print(f"  Lots: {lots}")
    print(f"  Pricing: LTP (Last Traded Price)")
    print(f"{'='*70}\n")
    
    storage = ChainStorage()
    
    # Get available date range if not specified
    if not start_date or not end_date:
        date_range = storage.get_date_range(symbol)
        if date_range:
            start_date = start_date or date_range[0]
            end_date = end_date or date_range[1]
            print(f"Using available data range: {start_date} to {end_date}\n")
    
    if not start_date or not end_date:
        print(f"❌ No data available for {symbol}")
        return
    
    # Convert to datetime
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    trades = []
    current_date = start_dt
    
    while current_date <= end_dt:
        date_str = current_date.strftime("%Y-%m-%d")
        
        # Get chain for entry
        chains = storage.get_chains_on_date(symbol, current_date.date())
        
        if not chains:
            current_date += timedelta(days=1)
            continue
        
        # Use the first chain of the day
        entry_chain_obj = chains[0]
        
        # Convert to dict format for our functions
        entry_chain = {
            "spot": entry_chain_obj.spot_price,
            "strikes": []
        }
        
        for strike_obj in entry_chain_obj.strikes:
            entry_chain["strikes"].append({
                "strike": strike_obj.strike,
                "call": {
                    "ltp": strike_obj.call_ltp,
                    "bid": strike_obj.call_bid,
                    "ask": strike_obj.call_ask,
                    "oi": strike_obj.call_oi,
                    "volume": strike_obj.call_volume,
                    "lot_size": 50,  # NIFTY lot size (would need to be dynamic for other symbols)
                },
                "put": {
                    "ltp": strike_obj.put_ltp,
                    "bid": strike_obj.put_bid,
                    "ask": strike_obj.put_ask,
                    "oi": strike_obj.put_oi,
                    "volume": strike_obj.put_volume,
                    "lot_size": 50,
                }
            })
        
        if not entry_chain.get("strikes"):
            current_date += timedelta(days=1)
            continue
        
        # Build Iron Condor
        ic = build_iron_condor(entry_chain, otm_offset=otm_offset, 
                              wing_width=wing_width, lots=lots)
        
        if not ic:
            current_date += timedelta(days=1)
            continue
        
        # Calculate exit date
        exit_date = current_date + timedelta(days=hold_days)
        if exit_date > end_dt:
            break
        
        # Get chain at exit
        exit_chains = storage.get_chains_on_date(symbol, exit_date.date())
        
        if not exit_chains:
            current_date += timedelta(days=1)
            continue
        
        exit_spot = exit_chains[0].spot_price
        if not exit_spot:
            current_date += timedelta(days=1)
            continue
        
        # Calculate P&L
        pnl = calculate_pnl_at_expiry(ic, exit_spot)
        return_pct = (pnl / ic["max_loss"]) * 100 if ic["max_loss"] > 0 else 0
        
        trade = {
            "entry_date": date_str,
            "exit_date": exit_date.strftime("%Y-%m-%d"),
            "entry_spot": ic["spot"],
            "exit_spot": exit_spot,
            "premium_collected": ic["net_premium_total"],
            "max_profit": ic["max_profit"],
            "max_loss": ic["max_loss"],
            "pnl": pnl,
            "return_pct": return_pct,
            "winner": pnl > 0,
        }
        
        trades.append(trade)
        
        # Print trade
        status = "✅" if pnl > 0 else "❌"
        print(f"[{date_str}] {status} Entry: ₹{ic['spot']:.0f} → Exit: ₹{exit_spot:.0f} | "
              f"P&L: ₹{pnl:,.0f} ({return_pct:+.1f}%)")
        
        # Move to next week (don't overlap trades)
        current_date += timedelta(days=hold_days + 1)
    
    # Calculate statistics
    if not trades:
        print("\n❌ No trades executed")
        return
    
    total_trades = len(trades)
    winners = [t for t in trades if t["winner"]]
    losers = [t for t in trades if not t["winner"]]
    
    win_count = len(winners)
    loss_count = len(losers)
    win_rate = (win_count / total_trades) * 100
    
    avg_win = sum(t["pnl"] for t in winners) / win_count if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / loss_count if losers else 0
    
    total_pnl = sum(t["pnl"] for t in trades)
    total_return = sum(t["return_pct"] for t in trades)
    
    max_dd = 0
    running_pnl = 0
    peak = 0
    for t in trades:
        running_pnl += t["pnl"]
        peak = max(peak, running_pnl)
        drawdown = peak - running_pnl
        max_dd = max(max_dd, drawdown)
    
    # Sharpe ratio (simplified: returns / std dev)
    returns = [t["return_pct"] for t in trades]
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
    std_dev = variance ** 0.5
    sharpe = (mean_return / std_dev) if std_dev > 0 else 0
    
    profit_factor = abs(sum(t["pnl"] for t in winners) / sum(t["pnl"] for t in losers)) if losers else float('inf')
    
    # Print results
    print(f"\n{'='*70}")
    print(f"📊 BACKTEST RESULTS")
    print(f"{'='*70}\n")
    
    print(f"Total Trades:      {total_trades}")
    print(f"Winning Trades:    {win_count} ({win_rate:.1f}%)")
    print(f"Losing Trades:     {loss_count}\n")
    
    print(f"Average Win:       ₹{avg_win:,.0f}")
    print(f"Average Loss:      ₹{avg_loss:,.0f}")
    print(f"Win/Loss Ratio:    {abs(avg_win/avg_loss) if avg_loss != 0 else float('inf'):.2f}\n")
    
    print(f"Total P&L:         ₹{total_pnl:,.0f}")
    print(f"Total Return:      {total_return:+.1f}%")
    print(f"Avg Return/Trade:  {mean_return:+.1f}%")
    print(f"Max Drawdown:      ₹{max_dd:,.0f}")
    print(f"Sharpe Ratio:      {sharpe:.2f}\n")
    
    print(f"Profit Factor:     {profit_factor:.2f}")
    
    print(f"\n{'-'*70}")
    print(f"💡 Interpretation:\n")
    
    if total_return > 10:
        print("✅ Strong performance (>10% total return)")
    elif total_return > 5:
        print("⚠️  Moderate performance (5-10% total return)")
    else:
        print("❌ Weak performance (<5% total return)")
    
    if win_rate >= 65:
        print("✅ Excellent win rate (≥65%)")
    elif win_rate >= 55:
        print("✅ Good win rate (≥55%)")
    else:
        print("⚠️  Low win rate (<55%)")
    
    if sharpe >= 1.5:
        print("✅ Excellent risk-adjusted returns (Sharpe ≥1.5)")
    elif sharpe >= 1.0:
        print("✅ Good risk-adjusted returns (Sharpe ≥1.0)")
    else:
        print("⚠️  Weak risk-adjusted returns (Sharpe <1.0)")
    
    print(f"\n{'='*70}\n")
    
    return trades


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Iron Condor Backtest")
    parser.add_argument("--symbol", default="NIFTY", help="Underlying symbol")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--hold-days", type=int, default=7, help="Hold period in days")
    parser.add_argument("--otm-offset", type=int, default=100, help="OTM offset in points")
    parser.add_argument("--wing-width", type=int, default=100, help="Wing width in points")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots")
    
    args = parser.parse_args()
    
    run_backtest(
        symbol=args.symbol,
        start_date=args.start_date,
        end_date=args.end_date,
        hold_days=args.hold_days,
        otm_offset=args.otm_offset,
        wing_width=args.wing_width,
        lots=args.lots,
    )
