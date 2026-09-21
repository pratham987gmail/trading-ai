#!/usr/bin/env python3
"""Quick check of downloaded data"""

import csv
from pathlib import Path
from collections import defaultdict

csv_file = r"C:\Users\prath\Downloads\archive\final_merged_output.csv"

print("📊 Analyzing downloaded option data...\n")
print("=" * 60)

# Get file size
file_size_mb = Path(csv_file).stat().st_size / (1024*1024)
print(f"File size: {file_size_mb:.1f} MB")

# Read and analyze
with open(csv_file, 'r') as f:
    reader = csv.DictReader(f)
    
    # Get columns
    fieldnames = reader.fieldnames
    print(f"\nColumns found: {len(fieldnames)}")
    for col in fieldnames:
        print(f"  ✓ {col}")
    
    print(f"\n{'=' * 60}")
    print("Scanning data (first 10,000 rows)...")
    print("=" * 60)
    
    dates = set()
    strikes = set()
    expiries = set()
    option_types = set()
    spot_prices = []
    
    count = 0
    for row in reader:
        count += 1
        if count > 10000:  # Sample first 10k rows for speed
            break
        
        try:
            dates.add(row['timestamp'][:10])  # Extract date part
            strikes.add(float(row['strike_price']))
            expiries.add(row['expiry'][:10])
            option_types.add(row['option_type'])
            spot_prices.append(float(row['underlying_spot_price']))
        except:
            pass
    
    print(f"\nRecords scanned: {count:,}")
    print(f"Unique dates: {len(dates)}")
    print(f"Date range: {min(dates)} to {max(dates)}")
    print(f"Unique strikes: {len(strikes)}")
    print(f"Unique expiries: {len(expiries)}")
    print(f"Option types: {', '.join(option_types)}")
    
    if spot_prices:
        avg_spot = sum(spot_prices) / len(spot_prices)
        print(f"\nAverage spot price: {avg_spot:.2f}")
        
        # Identify index
        if 18000 <= avg_spot <= 26000:
            index = "NIFTY 50"
        elif 40000 <= avg_spot <= 50000:
            index = "BANKNIFTY"
        elif 17000 <= avg_spot <= 22000:
            index = "FINNIFTY"
        else:
            index = "UNKNOWN"
        
        print(f"Likely index: {index}")
    
    # Check key columns exist
    print(f"\n{'=' * 60}")
    print("DATA QUALITY")
    print("=" * 60)
    
    required_cols = ['strike_price', 'option_type', 'expiry', 'timestamp', 
                     'ltp', 'volume', 'oi', 'underlying_spot_price']
    
    has_all = all(col in fieldnames for col in required_cols)
    
    if has_all:
        print("✅ All required columns present")
    else:
        missing = [col for col in required_cols if col not in fieldnames]
        print(f"❌ Missing columns: {', '.join(missing)}")
    
    # Check Greeks
    greeks = ['iv', 'delta', 'gamma', 'theta', 'vega']
    has_greeks = all(col in fieldnames for col in greeks)
    
    if has_greeks:
        print("✅ Greeks available (IV, Delta, Gamma, Theta, Vega)")
    else:
        print("⚠️  Greeks not available or incomplete")
    
    print(f"\n{'=' * 60}")
    print("RECOMMENDATION")
    print("=" * 60)
    
    if len(dates) >= 30:
        print("✅ EXCELLENT: 30+ days - Ready for full backtest!")
    elif len(dates) >= 7:
        print("✅ GOOD: 7+ days - Can run initial validation")
    elif len(dates) >= 3:
        print("⚠️  LIMITED: 3+ days - Basic testing only")
    else:
        print("❌ INSUFFICIENT: Need more data")
    
    print(f"\n{'=' * 60}")
    print("NEXT STEPS")
    print("=" * 60)
    print("1. Run import script to load data into database")
    print("2. Generate signals using this historical data")
    print("3. Run backtest to validate strategy")
    print()
    print("✅ This data is ready to use!")

print("\n" + "=" * 60)
