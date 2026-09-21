"""
Live Data Collection Script

Collects real-time option chain data from Angel One and stores it in the database.
This data can later be used for backtesting strategies.

Usage:
    python scripts/collect_live_data.py --symbol NIFTY --interval 60

Options:
    --symbol: Symbol to collect (NIFTY, BANKNIFTY, FINNIFTY)
    --interval: Seconds between collections (default: 60)
    --duration: How long to collect in minutes (default: infinite)
"""

import sys
from pathlib import Path
import time
from datetime import datetime
import argparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Settings
from core.angel_client import AngelClient
from recorder.storage import ChainStorage


def collect_data(symbol="NIFTY", interval_seconds=60, duration_minutes=None):
    """Collect live option chain data."""
    
    print("\n" + "="*70)
    print(f"📊 LIVE DATA COLLECTION - {symbol}")
    print("="*70)
    print(f"Interval: Every {interval_seconds} seconds")
    if duration_minutes:
        print(f"Duration: {duration_minutes} minutes")
    else:
        print("Duration: Continuous (press Ctrl+C to stop)")
    print("="*70 + "\n")
    
    # Setup
    settings = Settings()
    client = AngelClient(settings)
    storage = ChainStorage()
    
    start_time = datetime.now()
    collection_count = 0
    error_count = 0
    
    print(f"🔌 Connected to Angel One")
    print(f"💾 Database: {storage.db_path}")
    print(f"🕐 Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    try:
        while True:
            try:
                # Check duration
                if duration_minutes:
                    elapsed = (datetime.now() - start_time).total_seconds() / 60
                    if elapsed >= duration_minutes:
                        print(f"\n✅ Collection complete ({duration_minutes} minutes)")
                        break
                
                # Collect chain
                now = datetime.now()
                print(f"[{now.strftime('%H:%M:%S')}] Fetching option chain...", end=" ")
                
                chain = client.get_option_chain(symbol)
                
                if chain and chain.get('strikes'):
                    # Convert to storage format
                    from recorder.storage import OptionChain, OptionChainStrike
                    
                    strikes = []
                    for s in chain.get('strikes', []):
                        call = s.get('call', {})
                        put = s.get('put', {})
                        
                        strike_obj = OptionChainStrike(
                            strike=s.get('strike'),
                            call_ltp=call.get('ltp'),
                            call_oi=call.get('oi', 0),
                            call_volume=call.get('volume', 0),
                            call_bid=call.get('bid'),
                            call_ask=call.get('ask'),
                            call_iv=call.get('iv'),
                            call_delta=call.get('delta'),
                            call_gamma=call.get('gamma'),
                            call_theta=call.get('theta'),
                            call_vega=call.get('vega'),
                            put_ltp=put.get('ltp'),
                            put_oi=put.get('oi', 0),
                            put_volume=put.get('volume', 0),
                            put_bid=put.get('bid'),
                            put_ask=put.get('ask'),
                            put_iv=put.get('iv'),
                            put_delta=put.get('delta'),
                            put_gamma=put.get('gamma'),
                            put_theta=put.get('theta'),
                            put_vega=put.get('vega'),
                        )
                        strikes.append(strike_obj)
                    
                    chain_obj = OptionChain(
                        symbol=symbol,
                        expiry=datetime.strptime(chain['expiry'], '%Y-%m-%d').date(),
                        spot_price=chain.get('spot'),
                        strikes=strikes,
                        timestamp=now,
                    )
                    
                    # Store it
                    storage.save_chain(chain_obj)
                    
                    collection_count += 1
                    print(f"✅ Saved ({len(strikes)} strikes, spot: ₹{chain.get('spot'):.2f})")
                    
                else:
                    error_count += 1
                    print(f"⚠️  No data received")
                
                # Show stats every 10 collections
                if collection_count % 10 == 0 and collection_count > 0:
                    elapsed_min = (datetime.now() - start_time).total_seconds() / 60
                    rate = collection_count / elapsed_min if elapsed_min > 0 else 0
                    print(f"\n📊 Stats: {collection_count} collections, {error_count} errors, {rate:.1f}/min\n")
                
                # Wait for next interval
                time.sleep(interval_seconds)
                
            except KeyboardInterrupt:
                print("\n\n⏹️  Stopped by user")
                break
                
            except Exception as e:
                error_count += 1
                print(f"❌ Error: {e}")
                time.sleep(interval_seconds)
    
    finally:
        # Final stats
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds() / 60
        
        print("\n" + "="*70)
        print("📊 COLLECTION SUMMARY")
        print("="*70)
        print(f"Started:     {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Ended:       {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Duration:    {duration:.1f} minutes")
        print(f"Collections: {collection_count}")
        print(f"Errors:      {error_count}")
        if duration > 0:
            print(f"Rate:        {collection_count / duration:.1f} collections/min")
        print("="*70)
        print()
        print("💾 Data stored in database and ready for backtesting!")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect live option chain data")
    parser.add_argument("--symbol", default="NIFTY", help="Symbol to collect")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between collections")
    parser.add_argument("--duration", type=int, help="Minutes to collect (default: infinite)")
    
    args = parser.parse_args()
    
    collect_data(args.symbol, args.interval, args.duration)
