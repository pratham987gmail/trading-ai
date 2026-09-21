#!/usr/bin/env python3
"""
Backtest with results saved to file
"""

import sys
from pathlib import Path
from datetime import datetime

# Import the simple backtest
sys.path.insert(0, str(Path(__file__).parent))
from simple_backtest import run_backtest

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Backtest with saved results")
    parser.add_argument("--symbol", default="NIFTY", help="Symbol")
    parser.add_argument("--start-date", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--hold-days", type=int, default=3, help="Days to hold")
    
    args = parser.parse_args()
    
    # Redirect output to file AND console
    results_dir = Path(__file__).parent.parent / "backtest_results"
    results_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_file = results_dir / f"backtest_{args.symbol}_{args.hold_days}d_{timestamp}.txt"
    
    # Save to file
    import sys
    from io import StringIO
    
    # Capture stdout
    old_stdout = sys.stdout
    sys.stdout = mystdout = StringIO()
    
    # Run backtest
    start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    run_backtest(args.symbol, start, end, args.hold_days)
    
    # Get output
    output = mystdout.getvalue()
    sys.stdout = old_stdout
    
    # Print to console
    print(output)
    
    # Save to file
    with open(result_file, 'w') as f:
        f.write(f"Backtest Results\n")
        f.write(f"{'=' * 70}\n")
        f.write(f"Symbol: {args.symbol}\n")
        f.write(f"Period: {args.start_date} to {args.end_date}\n")
        f.write(f"Hold Days: {args.hold_days}\n")
        f.write(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'=' * 70}\n\n")
        f.write(output)
    
    print(f"\n\nResults saved to: {result_file}")
