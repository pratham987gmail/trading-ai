"""Standard candidate interface for Phase 3 strategy generators.

Every generator in :mod:`research.strategies` implements :class:`Strategy`
and emits zero or more :class:`Candidate` objects. A ``Candidate`` is a
**research artifact**, not an order and not a broker call: nothing in this
package, or in any module built on top of it, may place, size, or route a
real or paper trade. That boundary is enforced structurally, not just by
convention — see the ``test_no_broker_imports`` test in
``tests/research/test_strategies.py``, which scans every file in this
package for imports of ``core.``, ``scripts.``, or the broker layer and
fails the build if one appears.

Conventions carried over from Phase 1/2 (``research/regime/engine.py``,
``analysis/indicators.py``, ``research/ml/targets.py``):

- "Absence, not a default": when a strategy's preconditions are not met
  (insufficient bars, a missing column, a regime gate that isn't
  satisfied), it returns an empty list. It never fabricates a candidate
  from a partial window or a guessed value.
- Every tunable is a constructor/``params`` field, never a magic number
  baked into the logic, because the walk-forward optimizer elsewhere in
  this repo grid-searches these.
- Dataclasses carry an ``as_dict()`` method for JSON-safe serialization.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

Direction = str  # "BULLISH" | "BEARISH" | "NEUTRAL" — kept as str, not Literal,
# so a strategy can emit a value without importing a shared enum module that
# doesn't exist yet in this phase; callers that need strict validation can
# check membership in {"BULLISH", "BEARISH", "NEUTRAL"} themselves.


@dataclass(frozen=True)
class Candidate:
    """One strategy's opinion, at one instant, for one underlying.

    This is a candidate *object* for research — never an order, never a
    broker call. Nothing about acting on it (sizing, routing, submission)
    lives in this class or anywhere in ``research/strategies``.

    Attributes:
        candidate_id: Stable identifier, conventionally
            ``f"{strategy}-{timestamp.isoformat()}-{underlying}"``.
        timestamp: The bar timestamp this candidate was evaluated at.
        underlying: Underlying symbol, e.g. ``"NIFTY"``.
        strategy: Strategy name, e.g. ``"MOMENTUM"``.
        strategy_version: Semantic version of the strategy's own logic, so a
            later change to its rules doesn't silently reinterpret old
            recorded candidates.
        direction: ``"BULLISH"``, ``"BEARISH"``, or ``"NEUTRAL"``.
        regime: A ``RegimeLabel`` string from ``research.regime.engine``, or
            ``None`` when the strategy was not given a regime to evaluate
            against.
        candidate_score: 0-100, this module's own confidence in the
            candidate. Deliberately **not** the same scale or meaning as
            ``analysis/setup_scoring.py``'s score — that module scores a
            priced options structure; this one scores a bar-level or
            chain-level signal before any structure has been selected or
            priced.
        features: The feature values that produced this candidate, kept for
            audit — a reviewer should be able to see exactly which numbers
            drove the call without re-running the strategy.
        params: The strategy parameters used to produce this candidate.
        rationale: Short, human-readable explanation of why.
    """

    candidate_id: str
    timestamp: datetime
    underlying: str
    strategy: str
    strategy_version: str
    direction: Direction
    regime: str | None
    candidate_score: float
    features: dict[str, float]
    params: dict[str, Any]
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "timestamp": self.timestamp.isoformat()
            if isinstance(self.timestamp, datetime)
            else str(self.timestamp),
            "underlying": self.underlying,
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "direction": self.direction,
            "regime": self.regime,
            "candidate_score": self.candidate_score,
            "features": dict(self.features),
            "params": dict(self.params),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class StrategyContext:
    """Everything a bar-level strategy needs to evaluate one instant.

    Args:
        frame: OHLCV history up to and including "now", ascending
            DatetimeIndex. **The caller is responsible for look-ahead
            safety**: ``frame`` must not contain any bar after ``at``. This
            class does not (and cannot, without recomputing indicators
            itself) verify that every row respects the cutoff; it only
            checks that the frame's own last timestamp is not later than
            ``at``, which catches the common mistake of handing a strategy
            the full, un-truncated history.
        underlying: Underlying symbol.
        at: The instant being evaluated. Should equal ``frame.index[-1]``
            for a bar-aligned caller; a small tolerance is allowed so a
            slightly-stale ``at`` (e.g. "now" ticking a few seconds ahead of
            the last completed bar) does not hard-fail construction.
        regime_state: The current confirmed regime, or ``None`` when the
            caller hasn't evaluated one. Some strategies (mean-reversion
            modes) refuse to emit anything without this.
    """

    frame: pd.DataFrame
    underlying: str
    at: datetime
    regime_state: Any = None  # research.regime.engine.RegimeState | None

    def __post_init__(self) -> None:
        if self.frame is not None and not self.frame.empty and isinstance(
            self.frame.index, pd.DatetimeIndex
        ):
            last = self.frame.index[-1]
            at = self.at
            # Normalize tz-awareness mismatches defensively rather than
            # raising on a comparison TypeError; look-ahead safety is a
            # semantic check, not a formatting one.
            try:
                if last > at:
                    raise ValueError(
                        f"StrategyContext.frame's last bar ({last}) is after "
                        f"`at` ({at}) — this looks like un-truncated history "
                        "being handed to a strategy, which would let it see "
                        "the future. Truncate the frame to `at` before "
                        "constructing StrategyContext."
                    )
            except TypeError:
                # Naive vs. aware timestamps; not this constructor's job to
                # fix timezone handling, only to avoid a spurious crash.
                pass


class Strategy(ABC):
    """Base class for every Phase 3 candidate generator.

    Implementations must not import anything from ``core/``, ``scripts/``,
    or the broker layer — see the module docstring.
    """

    @abstractmethod
    def generate_candidates(self, context: StrategyContext) -> list[Candidate]:
        """Evaluate the context and return zero or more candidates.

        Returns an empty list, never a fabricated candidate, when the
        strategy's own preconditions (minimum bars, required columns,
        regime gate) are not met.
        """
        raise NotImplementedError
