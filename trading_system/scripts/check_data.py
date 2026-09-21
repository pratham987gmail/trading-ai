"""Check what data we have and why Iron Condor can't be built"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage

def check_data():
    print("\n" + "="*70)
    print("DATA DIAGNOSTICS")
    print("="*70 + "\n")
    
    storage = ChainStorage()
    
    # Check date range
    date_range = storage.get_date_range("NIFTY")
    if not date_range or not date_range[0]:
        print("❌ NO DATA FOUND for NIFTY")
        print("\nDid you import the CSV data?")
        print("Run: python scripts/import_downloaded_csv.py")
        return
    
    print(f"✅ Data available: {date_range[0]} to {date_range[1]}")
    
    # Get a sample chain
    sample_date = datetime.strptime(date_range[0], '%Y-%m-%d').date()
    chains = storage.get_chains_on_date("NIFTY", sample_date)
    
    if not chains:
        print(f"\n❌ No chains found for {sample_date}")
        return
    
    print(f"\n✅ Found {len(chains)} chain(s) on {sample_date}")
    
    chain = chains[0]
    print(f"\nChain Details:")
    print(f"  Symbol: {chain.symbol}")
    print(f"  Spot: ₹{chain.spot_price:,.2f}")
    print(f"  Expiry: {chain.expiry}")
    print(f"  Timestamp: {chain.timestamp}")
    print(f"  Strikes: {len(chain.strikes)}")
    
    # Show strike range
    if chain.strikes:
        strikes = [s.strike for s in chain.strikes]
        print(f"  Strike Range: {min(strikes)} to {max(strikes)}")
        
        # Show sample strikes
        print(f"\n  Sample Strikes (first 5):")
        for i, strike_obj in enumerate(chain.strikes[:5]):
            print(f"    {strike_obj.strike}: ", end="")
            
            # Check call data
            if strike_obj.call_bid and strike_obj.call_ask:
                print(f"Call bid={strike_obj.call_bid}, ask={strike_obj.call_ask} | ", end="")
            else:
                print(f"Call NO BID/ASK | ", end="")
            
            # Check put data
            if strike_obj.put_bid and strike_obj.put_ask:
                print(f"Put bid={strike_obj.put_bid}, ask={strike_obj.put_ask}")
            else:
                print(f"Put NO BID/ASK")
        
        # Check if we have bid/ask prices
        has_call_prices = sum(1 for s in chain.strikes if s.call_bid and s.call_ask)
        has_put_prices = sum(1 for s in chain.strikes if s.put_bid and s.put_ask)
        
        print(f"\n  Strikes with Call prices: {has_call_prices}/{len(chain.strikes)}")
        print(f"  Strikes with Put prices: {has_put_prices}/{len(chain.strikes)}")
        
        if has_call_prices < 10 or has_put_prices < 10:
            print("\n⚠️  WARNING: Very few strikes have bid/ask prices!")
            print("   This might be why Iron Condor can't be built.")
            
            # Check LTP as fallback
            has_call_ltp = sum(1 for s in chain.strikes if s.call_ltp)
            has_put_ltp = sum(1 for s in chain.strikes if s.put_ltp)
            print(f"\n  Strikes with Call LTP: {has_call_ltp}/{len(chain.strikes)}")
            print(f"  Strikes with Put LTP: {has_put_ltp}/{len(chain.strikes)}")
            
            if has_call_ltp > 10 and has_put_ltp > 10:
                print("\n  ✅ LTP data available - we can use that instead!")
        
        # Try to build Iron Condor manually
        spot = chain.spot_price
        atm = min(chain.strikes, key=lambda s: abs(s.strike - spot)).strike
        
        print(f"\n  ATM Strike: {atm} (spot @ {spot})")
        
        # Find strikes for IC
        otm_offset = 100
        wing_width = 100
        
        sell_call_strike = atm + otm_offset
        buy_call_strike = atm + otm_offset + wing_width
        sell_put_strike = atm - otm_offset
        buy_put_strike = atm - otm_offset - wing_width
        
        print(f"\n  Iron Condor Structure (OTM={otm_offset}, Wing={wing_width}):")
        print(f"    Buy Call:  {buy_call_strike}")
        print(f"    Sell Call: {sell_call_strike}")
        print(f"    Sell Put:  {sell_put_strike}")
        print(f"    Buy Put:   {buy_put_strike}")
        
        # Check if these strikes exist
        available_strikes = set(s.strike for s in chain.strikes)
        
        missing = []
        if buy_call_strike not in available_strikes:
            missing.append(f"Buy Call {buy_call_strike}")
        if sell_call_strike not in available_strikes:
            missing.append(f"Sell Call {sell_call_strike}")
        if sell_put_strike not in available_strikes:
            missing.append(f"Sell Put {sell_put_strike}")
        if buy_put_strike not in available_strikes:
            missing.append(f"Buy Put {buy_put_strike}")
        
        if missing:
            print(f"\n  ❌ PROBLEM: Missing strikes: {', '.join(missing)}")
            print(f"\n  Available strikes: {sorted(available_strikes)}")
            print(f"\n  💡 SOLUTION: Try smaller offsets!")
            print(f"     --otm-offset 50 --wing-width 50")
        else:
            print(f"\n  ✅ All strikes exist!")
            
            # Check if they have prices
            for s in chain.strikes:
                if s.strike == sell_call_strike:
                    if not (s.call_bid and s.call_ask):
                        print(f"  ⚠️  Sell Call {sell_call_strike} has no bid/ask")
                if s.strike == buy_call_strike:
                    if not (s.call_bid and s.call_ask):
                        print(f"  ⚠️  Buy Call {buy_call_strike} has no bid/ask")
                if s.strike == sell_put_strike:
                    if not (s.put_bid and s.put_ask):
                        print(f"  ⚠️  Sell Put {sell_put_strike} has no bid/ask")
                if s.strike == buy_put_strike:
                    if not (s.put_bid and s.put_ask):
                        print(f"  ⚠️  Buy Put {buy_put_strike} has no bid/ask")
    
    print("\n" + "="*70)

if __name__ == "__main__":
    check_data()
