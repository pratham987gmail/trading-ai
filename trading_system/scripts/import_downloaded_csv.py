#!/usr/bin/env python3
"""
Import downloaded option chain CSV into recorder database

Usage:
    python scripts/import_downloaded_csv.py
"""

import sys
from pathlib import Path
import csv
from datetime import datetime
from collections import defaultdict

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage
from analysis.signal_models import OptionChain, OptionChainStrike


def parse_csv_to_chains(csv_file: str) -> dict:
    """
    Parse CSV and group by (timestamp, expiry).
    Returns dict of OptionChain objects.
    """
    print(f"\n[IMPORT] Starting: {csv_file}")
    print("=" * 60)
    
    # Group data by timestamp and expiry
    chains_data = defaultdict(lambda: {
        'spot_price': None,
        'strikes': defaultdict(lambda: {'CE': {}, 'PE': {}})
    })
    
    count = 0
    skipped = 0
    
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            count += 1
            if count % 10000 == 0:
                print(f"  Processed {count:,} rows...")
            
            try:
                # Parse key fields
                timestamp_str = row['timestamp']
                expiry_str = row['expiry']
                strike = float(row['strike_price'])
                option_type = row['option_type']  # CE or PE
                
                # Parse timestamp and expiry
                # Format: 2024-10-03 15:29:00
                timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                expiry = datetime.strptime(expiry_str, '%Y-%m-%d %H:%M:%S')
                
                # Create key (timestamp + expiry)
                # Round to nearest minute for grouping
                ts_key = timestamp.replace(second=0, microsecond=0)
                key = (ts_key, expiry.date())
                
                # Store spot price
                if chains_data[key]['spot_price'] is None:
                    chains_data[key]['spot_price'] = float(row['underlying_spot_price'])
                
                # Parse option data
                option_data = {
                    'ltp': float(row['ltp']) if row['ltp'] else None,
                    'volume': int(float(row['volume'])) if row['volume'] else 0,
                    'oi': int(float(row['oi'])) if row['oi'] else 0,
                    'iv': float(row['iv']) if row.get('iv') and row['iv'] else None,
                    'delta': float(row['delta']) if row.get('delta') and row['delta'] else None,
                    'gamma': float(row['gamma']) if row.get('gamma') and row['gamma'] else None,
                    'theta': float(row['theta']) if row.get('theta') and row['theta'] else None,
                    'vega': float(row['vega']) if row.get('vega') and row['vega'] else None,
                    'bid': None,  # Not in this dataset
                    'ask': None   # Not in this dataset
                }
                
                # Store in appropriate CE/PE slot
                chains_data[key]['strikes'][strike][option_type] = option_data
                
            except Exception as e:
                skipped += 1
                if skipped < 10:  # Only show first 10 errors
                    print(f"  [WARN] Skipped row {count}: {e}")
                continue
    
    print(f"\n[OK] Parsed {count:,} rows ({skipped:,} skipped)")
    print(f"[INFO] Found {len(chains_data):,} unique chains")
    
    # Convert to OptionChain objects
    chains = []
    
    print("\n[BUILDING] Building OptionChain objects...")
    
    skipped_no_spot = 0
    
    for (timestamp, expiry), data in chains_data.items():
        # Skip chains with no spot price
        spot = data['spot_price']
        if spot is None or spot == 0:
            skipped_no_spot += 1
            continue
        
        # Filter by known indices only
        if not (18000 <= spot <= 26000 or 40000 <= spot <= 50000 or 17000 <= spot <= 22000):
            skipped_no_spot += 1
            continue
        
        strikes_list = []
        
        for strike, options in data['strikes'].items():
            ce_data = options.get('CE', {})
            pe_data = options.get('PE', {})
            
            strike_obj = OptionChainStrike(
                strike=strike,
                call_ltp=ce_data.get('ltp'),
                call_oi=ce_data.get('oi', 0),
                call_volume=ce_data.get('volume', 0),
                call_iv=ce_data.get('iv'),
                call_delta=ce_data.get('delta'),
                call_gamma=ce_data.get('gamma'),
                call_theta=ce_data.get('theta'),
                call_vega=ce_data.get('vega'),
                call_bid=ce_data.get('bid'),
                call_ask=ce_data.get('ask'),
                put_ltp=pe_data.get('ltp'),
                put_oi=pe_data.get('oi', 0),
                put_volume=pe_data.get('volume', 0),
                put_iv=pe_data.get('iv'),
                put_delta=pe_data.get('delta'),
                put_gamma=pe_data.get('gamma'),
                put_theta=pe_data.get('theta'),
                put_vega=pe_data.get('vega'),
                put_bid=pe_data.get('bid'),
                put_ask=pe_data.get('ask')
            )
            
            strikes_list.append(strike_obj)
        
        # Determine symbol based on spot price (already validated above)
        if 18000 <= spot <= 26000:
            symbol = "NIFTY"
        elif 40000 <= spot <= 50000:
            symbol = "BANKNIFTY"
        else:  # 17000 <= spot <= 22000
            symbol = "FINNIFTY"
        
        chain = OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=spot,
            strikes=sorted(strikes_list, key=lambda s: s.strike),
            timestamp=timestamp
        )
        
        chains.append(chain)
    
    if skipped_no_spot > 0:
        print(f"[INFO] Skipped {skipped_no_spot:,} chains (no spot price or unknown index)")
    
    return chains


def import_to_database(chains: list, storage: ChainStorage):
    """Save chains to database"""
    print(f"\n[DATABASE] Importing {len(chains):,} chains to database...")
    print("=" * 60)
    
    imported = 0
    duplicates = 0
    errors = 0
    
    for i, chain in enumerate(chains, 1):
        if i % 1000 == 0:
            pct = (i / len(chains)) * 100
            print(f"  Progress: {i:,}/{len(chains):,} ({pct:.1f}%) - {imported:,} imported, {duplicates:,} duplicates")
        
        try:
            storage.save_chain(chain)
            imported += 1
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                duplicates += 1
            else:
                errors += 1
                if errors < 5:
                    print(f"  [WARN] Error saving chain: {e}")
    
    print(f"\n{'=' * 60}")
    print("IMPORT SUMMARY")
    print("=" * 60)
    print(f"[OK] Imported: {imported:,} chains")
    print(f"[SKIP] Duplicates skipped: {duplicates:,}")
    print(f"[ERROR] Errors: {errors:,}")
    
    return imported


def main():
    csv_file = r"C:\Users\prath\Downloads\archive\final_merged_output.csv"
    
    print("\n" + "=" * 60)
    print("OPTION CHAIN DATA IMPORT")
    print("=" * 60)
    
    # Check file exists
    if not Path(csv_file).exists():
        print(f"[ERROR] File not found: {csv_file}")
        sys.exit(1)
    
    # Parse CSV
    chains = parse_csv_to_chains(csv_file)
    
    if not chains:
        print("[ERROR] No chains found in CSV")
        sys.exit(1)
    
    # Get date range
    dates = set(chain.timestamp.date() for chain in chains)
    symbols = set(chain.symbol for chain in chains)
    
    print(f"\n[DATA SUMMARY]")
    print("=" * 60)
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Date range: {min(dates)} to {max(dates)}")
    print(f"Trading days: {len(dates)}")
    print(f"Total chains: {len(chains):,}")
    print()
    
    # Initialize storage
    storage = ChainStorage()
    
    # Import
    imported = import_to_database(chains, storage)
    
    print(f"\n[OK] IMPORT COMPLETE!")
    print("=" * 60)
    print(f"Database ready for backtesting with {imported:,} chains")
    print()
    print("Next steps:")
    print("1. Run: python scripts/backtest_signals.py --symbol NIFTY --start-date 2024-10-01 --end-date 2024-10-31")
    print("2. Or generate live signals: python -m uvicorn api.main:app --reload")
    print()


if __name__ == "__main__":
    main()
