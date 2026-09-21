"""Phase 3 candidate generators.

Every strategy in this package produces :class:`~research.strategies.candidate.Candidate`
objects for research only — never orders, never broker calls, never anything
that touches paper or live trading. See ``candidate.py``'s module docstring
for the full contract.
"""

from research.strategies.candidate import Candidate, Strategy, StrategyContext

__all__ = ["Candidate", "Strategy", "StrategyContext"]
