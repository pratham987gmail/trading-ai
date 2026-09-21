"""Research CLI: print the strategy leaderboard.

Usage:
    python -m research.cli.leaderboard [--path research_data/leaderboard.json] [--by expectancy]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from research.experiments.leaderboard import Leaderboard  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="research_data/leaderboard.json")
    parser.add_argument("--by", default="expectancy")
    args = parser.parse_args(argv)

    board = Leaderboard(Path(args.path))
    rows = board.ranked(by=args.by)
    if not rows:
        print(f"No leaderboard entries at {args.path} yet.")
        return 0

    header = f"{'Strategy':<20}{'Status':<14}{'Trades':>8}{'Expectancy':>14}{'PF':>8}{'Sharpe':>8}{'WinRate':>10}"
    print(header)
    print("-" * len(header))
    for row in rows:
        pf = f"{row.profit_factor:.2f}" if row.profit_factor is not None else "-"
        sharpe = f"{row.sharpe:.2f}" if row.sharpe is not None else "-"
        print(
            f"{row.strategy:<20}{row.status:<14}{row.trades:>8}{row.expectancy:>14.2f}"
            f"{pf:>8}{sharpe:>8}{row.win_rate:>10.1%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
