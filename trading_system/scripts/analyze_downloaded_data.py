#!/usr/bin/env python3
"""Quick analysis of downloaded option chain data"""

import pandas as pd
from pathlib import Path

csv_file = r"C:\Users\prath\Downloads\archive\final_merged_output.csv"

print("📊 Analyzing downloaded option data...\n")

# Read first few rows to understand structure
df = pd.read_csv(csv_file, nrows=1000)

print(f"{'=' * 60}")
print("FILE INFO")
print(f"{'=' * 60}")
print(f"File: {csv_file}")
print(f"Size: {Path(csv_file).stat().st_size / (1024*1024):.1f} MB")
print()

print(f"{'=' * 60}")
print("COLUMNS AVAILABLE")
print(f"{'=' * 60}")
for col in df.columns:
    print(f"  ✓ {col}")
print()

print(f"{'=' * 60}")
print("DATA SAMPLE")
print(f"{'=' * 60}")
print(df.head(3).to_string())
print()

print(f"{'=' * 60}")
print("DATA STATISTICS")
print(f"{'=' * 60}")

# Read full file for stats
print("Loading full dataset... (may take 30-60 seconds)")
df_full = pd.read_csv(csv_file)

print(f"\nTotal records: {len(df_full):,}")
print(f"Date range: {df_full['timestamp'].min()} to {df_full['timestamp'].max()}")
print(f"Unique strikes: {df_full['strike_price'].nunique()}")
print(f"Option types: {df_full['option_type'].unique()}")
print(f"Unique expiries: {df_full['expiry'].nunique()}")
print()

# Check what underlying this is
df_full['timestamp'] = pd.to_datetime(df_full['timestamp'])
df_full['expiry'] = pd.to_datetime(df_full['expiry'])

spot_prices = df_full['underlying_spot_price'].unique()
avg_spot = df_full['underlying_spot_price'].mean()

print(f"Spot price range: {spot_prices.min():.2f} - {spot_prices.max():.2f}")
print(f"Average spot: {avg_spot:.2f}")

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
print()

# Date coverage
dates = df_full['timestamp'].dt.date.unique()
print(f"Trading days covered: {len(dates)}")
print(f"First day: {dates.min()}")
print(f"Last day: {dates.max()}")
print()

# Check if we have Greeks
has_greeks = all(col in df_full.columns for col in ['iv', 'delta', 'gamma', 'theta', 'vega'])
print(f"Greeks available: {'✅ YES' if has_greeks else '❌ NO'}")
print(f"Bid-Ask available: {'✅ YES' if 'bid' in df_full.columns else '❌ NO'}")
print()

print(f"{'=' * 60}")
print("QUALITY CHECK")
print(f"{'=' * 60}")

# Check for missing data
print(f"Missing LTP: {df_full['ltp'].isna().sum():,} ({df_full['ltp'].isna().sum()/len(df_full)*100:.1f}%)")
print(f"Missing OI: {df_full['oi'].isna().sum():,} ({df_full['oi'].isna().sum()/len(df_full)*100:.1f}%)")
print(f"Missing Volume: {df_full['volume'].isna().sum():,} ({df_full['volume'].isna().sum()/len(df_full)*100:.1f}%)")
print(f"Missing IV: {df_full['iv'].isna().sum():,} ({df_full['iv'].isna().sum()/len(df_full)*100:.1f}%)")
print()

print(f"{'=' * 60}")
print("RECOMMENDATION")
print(f"{'=' * 60}")

if len(dates) >= 30:
    print("✅ EXCELLENT: 30+ days of data - Ready for comprehensive backtest!")
elif len(dates) >= 7:
    print("✅ GOOD: 7+ days of data - Ready for initial backtest validation")
elif len(dates) >= 3:
    print("⚠️  FAIR: 3+ days of data - Can test basic functionality")
else:
    print("❌ INSUFFICIENT: Need more data for meaningful backtest")

print()
print("Next step: Run import script to load into database")
print(f"Command: python scripts/import_custom_csv.py")
