"""
Simple Live Data Collector - Direct Angel One Integration

Collects option chain data and stores it in your database.
Cloud-ready with enhanced logging and error handling.
"""

import sys
from pathlib import Path
import os
import time
import traceback
from datetime import datetime, date
import argparse
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment
load_dotenv()

# Cloud logging helper
def log(level, message):
    """Log with timestamp and level for cloud monitoring."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {message}", flush=True)


def collect_live_data(symbol="NIFTY", interval_seconds=60, duration_minutes=None):
    """Collect live option chain data with cloud-ready error handling."""
    
    from SmartApi import SmartConnect
    import pyotp
    from recorder.storage import ChainStorage, OptionChain, OptionChainStrike
    
    log("INFO", "="*70)
    log("INFO", f"DATA COLLECTION SERVICE STARTING - {symbol}")
    log("INFO", "="*70)
    log("INFO", f"Interval: Every {interval_seconds} seconds")
    if duration_minutes:
        log("INFO", f"Duration: {duration_minutes} minutes")
    else:
        log("INFO", "Duration: Continuous (24/7 cloud mode)")
    log("INFO", "="*70)
    
    # Get credentials
    api_key = os.getenv('ANGEL_API_KEY')
    client_id = os.getenv('ANGEL_CLIENT_ID')
    password = os.getenv('ANGEL_PASSWORD')
    totp_secret = os.getenv('ANGEL_TOTP_SECRET')
    
    if not all([api_key, client_id, password, totp_secret]):
        log("ERROR", "Missing Angel One credentials in environment variables")
        raise Exception("Missing credentials")
    
    # Setup SmartAPI with retry
    smartApi = None
    for attempt in range(1, 4):
        try:
            log("INFO", f"Connecting to Angel One (attempt {attempt}/3)...")
            totp = pyotp.TOTP(totp_secret).now()
            smartApi = SmartConnect(api_key=api_key)
            data = smartApi.generateSession(client_id, password, totp)
            
            if not data.get('status'):
                raise Exception(f"Authentication failed: {data.get('message')}")
            
            log("INFO", f"✅ Connected successfully (Client: {client_id})")
            break
        except Exception as e:
            log("ERROR", f"Connection attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                raise
    
    # Setup storage
    storage = ChainStorage()
    log("INFO", f"💾 Database: {storage.db_path}")
    
    # Symbol mapping
    symbol_tokens = {
        "NIFTY": "99926000",
        "BANKNIFTY": "99926009",
        "FINNIFTY": "99926037",
    }
    
    exchange_symbol = {
        "NIFTY": "NIFTY 50",
        "BANKNIFTY": "BANKNIFTY",
        "FINNIFTY": "FINNIFTY",
    }
    
    token = symbol_tokens.get(symbol)
    exch_sym = exchange_symbol.get(symbol, symbol)
    
    start_time = datetime.now()
    collection_count = 0
    error_count = 0
    consecutive_errors = 0
    max_consecutive_errors = 10
    
    log("INFO", f"🕐 Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        while True:
            try:
                # Check if too many consecutive errors
                if consecutive_errors >= max_consecutive_errors:
                    log("ERROR", f"Too many consecutive errors ({consecutive_errors}), stopping...")
                    break
                
                # Check duration
                if duration_minutes:
                    elapsed = (datetime.now() - start_time).total_seconds() / 60
                    if elapsed >= duration_minutes:
                        log("INFO", f"Collection complete ({duration_minutes} minutes)")
                        break
                
                now = datetime.now()
                
                # Get spot price
                ltp_data = smartApi.ltpData("NSE", exch_sym, token)
                spot = ltp_data['data']['ltp'] if ltp_data and ltp_data.get('data') else None
                
                if not spot:
                    log("WARNING", "No spot price available")
                    error_count += 1
                    consecutive_errors += 1
                    time.sleep(interval_seconds)
                    continue
                
                consecutive_errors = 0  # Reset on success
                
                # Get option chain (simplified - we'll get all available strikes)
                # Note: Angel One doesn't have a single "get option chain" API
                # You need to fetch individual strikes or use websocket
                # For now, we'll demonstrate with basic structure
                
                # Create a basic chain with current spot
                # In production, you'd fetch actual strike data
                strikes = []
                
                # Generate common strikes around spot (every 50 points)
                atm = round(spot / 50) * 50
                for offset in range(-500, 550, 50):
                    strike_price = atm + offset
                    
                    # In production, fetch real data for each strike here
                    # For demo, creating placeholder
                    strike_obj = OptionChainStrike(
                        strike=strike_price,
                        call_ltp=None,  # Would fetch real data
                        call_oi=0,
                        call_volume=0,
                        put_ltp=None,
                        put_oi=0,
                        put_volume=0,
                    )
                    strikes.append(strike_obj)
                
                # Get next Thursday (typical expiry)
                today = date.today()
                days_ahead = 3 - today.weekday()  # Thursday = 3
                if days_ahead <= 0:
                    days_ahead += 7
                from datetime import timedelta
                expiry_date = today + timedelta(days=days_ahead)
                
                # Create chain object
                chain_obj = OptionChain(
                    symbol=symbol,
                    expiry=expiry_date,
                    spot_price=spot,
                    strikes=strikes,
                    timestamp=now,
                )
                
                # Store it
                storage.save_chain(chain_obj)
                
                collection_count += 1
                log("INFO", f"✅ Collected: spot ₹{spot:.2f}, {len(strikes)} strikes, saved to DB")
                
                # Show stats every 10 collections
                if collection_count % 10 == 0 and collection_count > 0:
                    elapsed_min = (datetime.now() - start_time).total_seconds() / 60
                    rate = collection_count / elapsed_min if elapsed_min > 0 else 0
                    log("INFO", f"📊 Stats: {collection_count} collections, {error_count} errors, {rate:.1f}/min")
                
                # Wait for next interval
                time.sleep(interval_seconds)
                
            except KeyboardInterrupt:
                log("INFO", "Stopped by user")
                break
                
            except Exception as e:
                error_count += 1
                consecutive_errors += 1
                log("ERROR", f"Collection error: {e}")
                log("ERROR", traceback.format_exc())
                
                # Try to reconnect after multiple errors
                if consecutive_errors >= 5:
                    log("WARNING", "Multiple errors, attempting reconnection...")
                    try:
                        totp = pyotp.TOTP(totp_secret).now()
                        smartApi = SmartConnect(api_key=api_key)
                        data = smartApi.generateSession(client_id, password, totp)
                        if data.get('status'):
                            log("INFO", "✅ Reconnected successfully")
                            consecutive_errors = 0
                    except Exception as reconnect_error:
                        log("ERROR", f"Reconnection failed: {reconnect_error}")
                
                time.sleep(interval_seconds)
    
    finally:
        # Final stats
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds() / 60
        
        log("INFO", "="*70)
        log("INFO", "COLLECTION SUMMARY")
        log("INFO", "="*70)
        log("INFO", f"Started:     {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        log("INFO", f"Ended:       {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        log("INFO", f"Duration:    {duration:.1f} minutes")
        log("INFO", f"Collections: {collection_count}")
        log("INFO", f"Errors:      {error_count}")
        if duration > 0:
            log("INFO", f"Rate:        {collection_count / duration:.1f} collections/min")
        log("INFO", "="*70)
        log("INFO", "💾 Data stored in database")
        log("INFO", "💡 Note: This collects spot prices and basic chain structure")
        log("INFO", "   For full option data with real premiums/Greeks, integrate Angel One option APIs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect live data from Angel One")
    parser.add_argument("--symbol", default="NIFTY", help="Symbol: NIFTY, BANKNIFTY, FINNIFTY")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between collections")
    parser.add_argument("--duration", type=int, help="Minutes to collect (default: infinite)")
    
    args = parser.parse_args()
    
    log("INFO", "="*70)
    log("INFO", "DATA COLLECTION SERVICE STARTING")
    log("INFO", f"Symbol: {args.symbol}")
    log("INFO", f"Interval: {args.interval} seconds")
    log("INFO", f"Duration: {'Continuous' if not args.duration else f'{args.duration} minutes'}")
    log("INFO", "="*70)
    
    try:
        collect_live_data(args.symbol, args.interval, args.duration)
    except Exception as e:
        log("ERROR", f"Data collection failed: {e}")
        log("ERROR", traceback.format_exc())
        sys.exit(1)
