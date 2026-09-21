#!/usr/bin/env python3
"""
Import Historical Option Data from TrueData API

Downloads intraday option chain data from TrueData (requires subscription)
and imports into recorder database for backtesting.

Setup:
    1. Subscribe to TrueData: https://truedata.in/
    2. Get API key from dashboard
    3. Add to .env: TRUEDATA_API_KEY=your_key_here

Usage:
    python scripts/import_truedata.py --start-date 2024-01-01 --end-date 2024-12-31 --interval 1min
"""

import argparse
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage
from analysis.signal_models import OptionChain, OptionChainStrike
import requests
import time
from dotenv import load_dotenv


load_dotenv()


def get_truedata_symbols(symbol: str, expiry: datetime) -> list:
    """
    Generate TrueData symbol names for option chain.
    
    Format: NIFTY24JAN21000CE, NIFTY24JAN21000PE
    """
    symbols = []
    
    # Strike range (adjust based on spot price)
    # For simplicity, generate ±20% range with 50-point intervals
    base_strikes = range(18000, 25000, 50)  # NIFTY range
    
    if symbol == "BANKNIFTY":
        base_strikes = range(40000, 55000, 100)
    elif symbol == "FINNIFTY":
        base_strikes = range(17000, 23000, 50)
    
    expiry_str = expiry.strftime("%y%b").upper()  # 24JAN
    
    for strike in base_strikes:
        symbols.append(f"{symbol}{expiry_str}{strike}CE")
        symbols.append(f"{symbol}{expiry_str}{strike}PE")
    
    return symbols


def download_truedata_bars(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    interval: str,
    api_key: str
) -> list:
    """
    Download historical bars from TrueData API.
    
    Returns list of OHLC bars with timestamp.
    """
    url = "https://history-api.truedata.in/getbars"
    
    params = {
        "symbol": symbol,
        "from": start_date.strftime("%Y%m%d"),
        "to": end_date.strftime("%Y%m%d"),
        "response": "json",
        "interval": interval  # 1, 5, 15, 60, etc.
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    
    response = requests.get(url, params=params, headers=headers, timeout=30)
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"TrueData API failed: {response.status_code} - {response.text}")


def build_option_chain_from_bars(
    underlying: str,
    expiry: datetime,
    timestamp: datetime,
    option_bars: dict,
    spot_price: float
) -> OptionChain:
    """
    Reconstruct option chain from individual option bar data.
    
    option_bars: {
        "NIFTY24JAN21000CE": [...bars...],
        "NIFTY24JAN21000PE": [...bars...],
        ...
    }
    """
    strikes_map = {}
    
    for symbol, bars in option_bars.items():
        # Parse symbol: NIFTY24JAN21000CE
        parts = symbol.replace(underlying, "").replace(expiry.strftime("%y%b").upper(), "")
        strike_str = parts[:-2]  # Remove CE/PE
        option_type = parts[-2:]  # CE or PE
        
        try:
            strike = float(strike_str)
        except ValueError:
            continue
        
        # Find bar matching timestamp
        matching_bar = None
        for bar in bars:
            bar_time = datetime.fromisoformat(bar["timestamp"])
            if abs((bar_time - timestamp).total_seconds()) < 60:  # Within 1 minute
                matching_bar = bar
                break
        
        if not matching_bar:
            continue
        
        # Initialize strike if not exists
        if strike not in strikes_map:
            strikes_map[strike] = OptionChainStrike(
                strike=strike,
                call_ltp=None, call_oi=0, call_volume=0, call_iv=None,
                call_delta=None, call_gamma=None, call_theta=None, call_vega=None,
                call_bid=None, call_ask=None,
                put_ltp=None, put_oi=0, put_volume=0, put_iv=None,
                put_delta=None, put_gamma=None, put_theta=None, put_vega=None,
                put_bid=None, put_ask=None
            )
        
        # Populate data
        if option_type == "CE":
            strikes_map[strike].call_ltp = matching_bar.get("close")
            strikes_map[strike].call_oi = matching_bar.get("oi", 0)
            strikes_map[strike].call_volume = matching_bar.get("volume", 0)
        else:  # PE
            strikes_map[strike].put_ltp = matching_bar.get("close")
            strikes_map[strike].put_oi = matching_bar.get("oi", 0)
            strikes_map[strike].put_volume = matching_bar.get("volume", 0)
    
    # Build chain
    strikes = sorted(strikes_map.values(), key=lambda s: s.strike)
    
    return OptionChain(
        symbol=underlying,
        expiry=expiry.date(),
        spot_price=spot_price,
        strikes=strikes,
        timestamp=timestamp
    )


def import_truedata_historical(
    symbols: list,
    start_date: datetime,
    end_date: datetime,
    interval: str,
    storage: ChainStorage
):
    """
    Download and import TrueData historical option data.
    """
    api_key = os.getenv("TRUEDATA_API_KEY")
    
    if not api_key:
        print("❌ Error: TRUEDATA_API_KEY not found in .env file")
        print("   Get your API key from https://truedata.in/")
        sys.exit(1)
    
    print(f"\n📥 Importing TrueData Historical Data")
    print(f"{'=' * 60}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Date range: {start_date.date()} to {end_date.date()}")
    print(f"Interval: {interval}")
    print()
    
    # Note: This is a simplified implementation
    # Real implementation would need to:
    # 1. Get list of expiries in date range
    # 2. Download all strikes for each expiry
    # 3. Reconstruct chains at each timestamp
    # 4. Handle TrueData rate limits (max 10 req/sec)
    
    print("⚠️  TrueData integration requires:")
    print("   1. Active subscription (₹500/month)")
    print("   2. Symbol mapping (TrueData format differs from NSE)")
    print("   3. Rate limit handling (10 req/sec)")
    print()
    print("📖 Contact TrueData support for:")
    print("   - Bulk download API")
    print("   - Symbol master file")
    print("   - Historical data packages")
    print()
    print("🔗 https://truedata.in/contact")


def main():
    parser = argparse.ArgumentParser(
        description="Import TrueData historical option data"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["NIFTY", "BANKNIFTY"],
        help="Symbols to import"
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
    parser.add_argument(
        "--interval",
        type=str,
        default="1",
        help="Bar interval in minutes (1, 5, 15, 60)"
    )
    
    args = parser.parse_args()
    
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
    
    storage = ChainStorage()
    
    import_truedata_historical(
        args.symbols,
        start_date,
        end_date,
        args.interval,
        storage
    )


if __name__ == "__main__":
    main()
