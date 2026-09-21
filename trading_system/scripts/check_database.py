#!/usr/bin/env python3
"""Check what's in the database"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from recorder.storage import ChainStorage

storage = ChainStorage()

print("\nDatabase Status")
print("=" * 60)

total = storage.get_chain_count()
print(f"Total chains in database: {total:,}")

if total == 0:
    print("\n[ERROR] Database is empty!")
    print("\nThe import didn't complete or failed.")
    print("Please run the import script again:")
    print("  python scripts/import_downloaded_csv.py")
else:
    print("\nData by symbol:")
    for symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
        first, last = storage.get_date_range(symbol)
        if first:
            print(f"  {symbol}: {first} to {last}")
        else:
            print(f"  {symbol}: No data")

print()
