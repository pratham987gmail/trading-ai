#!/usr/bin/env python3
"""
Import NSE End-of-Day Option Chain Historical Data

Downloads free historical option chain data from NSE and imports into
the recorder database format for backtesting.

Usage:
    python scripts/import_nse_eod.py --start-date 2024-01-01 --end-date 2024-12-31
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage
from broker.angel_one_client import AngelOneClient
from analysis.signal_models import OptionChain, OptionChainStrike
import requests
import time


def download_nse_option_chain(symbol: str, date: datetime) -> dict:
    """
    Download option chain data from NSE for a specific date.
    
    NSE provides EOD data, but requires cookies/headers to bypass bot detection.
    """
    # NSE option chain URL
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br"
    }
    
    session = requests.Session()
    
    # First request to get cookies
    session.get("https://www.nseindia.com", headers=headers)
    time.sleep(1)
    
    # Now get option chain
    response = session.get(url, headers=headers, timeout=10)
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"NSE API failed: {response.status_code}")


def parse_nse_to_option_chain(nse_data: dict, symbol: str, date: datetime) -> list:
    """
    Convert NSE JSON format to our OptionChain format.
    
    NSE provides multiple expiries in one response, so returns list of chains.
    """
    chains = []
    
    if "records" not in nse_data or "data" not in nse_data["records"]:
        return chains
    
    # Group by expiry
    expiry_map = {}
    spot_price = nse_data["records"].get("underlyingValue", 0)
    
    for item in nse_data["records"]["data"]:
        expiry_str = item.get("expiryDate")
        if not expiry_str:
            continue
        
        expiry = datetime.strptime(expiry_str, "%d-%b-%Y").date()
        
        if expiry not in expiry_map:
            expiry_map[expiry] = []
        
        strike = item.get("strikePrice", 0)
        
        # Call data
        call_data = item.get("CE", {})
        call_ltp = call_data.get("lastPrice")
        call_oi = call_data.get("openInterest", 0)
        call_volume = call_data.get("totalTradedVolume", 0)
        call_iv = call_data.get("impliedVolatility")
        call_bid = call_data.get("bidprice")
        call_ask = call_data.get("askPrice")
        
        # Put data
        put_data = item.get("PE", {})
        put_ltp = put_data.get("lastPrice")
        put_oi = put_data.get("openInterest", 0)
        put_volume = put_data.get("totalTradedVolume", 0)
        put_iv = put_data.get("impliedVolatility")
        put_bid = put_data.get("bidprice")
        put_ask = put_data.get("askPrice")
        
        # Create strike object
        strike_obj = OptionChainStrike(
            strike=strike,
            call_ltp=call_ltp,
            call_oi=int(call_oi) if call_oi else 0,
            call_volume=int(call_volume) if call_volume else 0,
            call_iv=call_iv,
            call_delta=None,  # NSE doesn't provide Greeks
            call_gamma=None,
            call_theta=None,
            call_vega=None,
            call_bid=call_bid,
            call_ask=call_ask,
            put_ltp=put_ltp,
            put_oi=int(put_oi) if put_oi else 0,
            put_volume=int(put_volume) if put_volume else 0,
            put_iv=put_iv,
            put_delta=None,
            put_gamma=None,
            put_theta=None,
            put_vega=None,
            put_bid=put_bid,
            put_ask=put_ask
        )
        
        expiry_map[expiry].append(strike_obj)
    
    # Build OptionChain objects
    timestamp = datetime.combine(date, datetime.min.time().replace(hour=15, minute=30))
    
    for expiry, strikes in expiry_map.items():
        chain = OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=spot_price,
            strikes=sorted(strikes, key=lambda s: s.strike),
            timestamp=timestamp
        )
        chains.append(chain)
    
    return chains


def import_historical_range(
    symbols: list,
    start_date: datetime,
    end_date: datetime,
    storage: ChainStorage
):
    """
    Download and import historical option chains for date range.
    """
    current_date = start_date
    total_days = (end_date - start_date).days
    imported_count = 0
    failed_count = 0
    
    print(f"\n📥 Importing NSE Historical Data")
    print(f"{'=' * 60}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Date range: {start_date.date()} to {end_date.date()}")
    print(f"Total days: {total_days}")
    print()
    
    while current_date <= end_date:
        # Skip weekends
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1)
            continue
        
        date_str = current_date.strftime("%Y-%m-%d")
        print(f"[{date_str}] Processing...", end=" ")
        
        day_success = 0
        day_fail = 0
        
        for symbol in symbols:
            try:
                # Download from NSE
                nse_data = download_nse_option_chain(symbol, current_date)
                
                # Parse to OptionChain objects
                chains = parse_nse_to_option_chain(nse_data, symbol, current_date)
                
                # Save to database
                for chain in chains:
                    storage.save_chain(chain)
                    day_success += 1
                
                time.sleep(2)  # Rate limit (NSE blocks aggressive scraping)
                
            except Exception as e:
                print(f"\n  ⚠️  {symbol} failed: {e}")
                day_fail += 1
        
        if day_success > 0:
            print(f"✅ {day_success} chains imported")
            imported_count += day_success
        else:
            print(f"❌ Failed")
            failed_count += 1
        
        current_date += timedelta(days=1)
    
    print()
    print(f"{'=' * 60}")
    print(f"✅ Import complete!")
    print(f"Total chains imported: {imported_count}")
    print(f"Failed days: {failed_count}")


def main():
    parser = argparse.ArgumentParser(
        description="Import NSE historical option chain data"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["NIFTY", "BANKNIFTY"],
        help="Symbols to import (default: NIFTY BANKNIFTY)"
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
    
    args = parser.parse_args()
    
    # Parse dates
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
    
    # Initialize storage
    storage = ChainStorage()
    
    # Import
    import_historical_range(args.symbols, start_date, end_date, storage)


if __name__ == "__main__":
    main()
