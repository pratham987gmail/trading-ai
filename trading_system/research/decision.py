"""The objective, rule-based research decision engine (Phase 3 spec section 28).

A strategy does not get called "robust" because it feels promising, and it
does not get called rejected because a run happened to lose money once. Every
verdict here follows the same fixed checklist, evaluated in the same order,
every time -- so two people (or the same person a month apart) looking at the
same inputs get the same :class:`ResearchVerdict`.

**Why the order matters.** Each gate answers a different question, and an
earlier gate can make a later one meaningless to even ask:

1. Not enough trades -> nothing below this point is statistically
   trustworthy, so we stop and say so rather than compute metrics that would
   look precise but are not.
2. Not enough distinct market regimes -> even with enough trades, a result
   drawn entirely from one regime (e.g. one trending month) cannot support a
   general claim of edge.
3. The charter's own deployment gate (profit factor, win rate, Sharpe) on
   this metrics -- reusing the *exact* thresholds already agreed in
   ``research/experiments/leaderboard.py`` rather than inventing new ones. A
   result that would not have cleared the charter's live-deployment bar
   cannot be quietly accepted here under different numbers.
4. Out-of-sample (walk-forward) failure -- a strategy that only performs
   in-sample is exactly the overfitting failure mode this phase exists to
   catch.
5. Fragility under cost/slippage stress -- an edge that evaporates under a
   realistic cost bump was never a tradeable edge.
6. Monte Carlo risk-of-ruin/drawdown gate failure -- the charter's tolerance
   for tail risk, not just average-case return.
7. Only once trade count and charter metrics both clear does it become
   possible to ask "was this actually validated end to end?" -- and if any of
   the three deeper checks (OOS, cost/slippage stress, Monte Carlo) was never
   run, the verdict is INCONCLUSIVE, not ACCEPT. A strategy must not be
   accepted on the strength of checks nobody ran.
8. Only when every gate was both run and passed does this return ACCEPT.

This module deliberately does not import a single "compute everything"
function -- validation methods live in ``research.backtest.*`` and
``research.robustness.*`` and are run separately; this module only combines
their already-computed verdicts into one final, auditable decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from research.experiments.leaderboard import MIN_PROFIT_FACTOR, MIN_SHARPE, MIN_WIN_RATE
from research.ml.evaluation import TradingMetrics

Decision = Literal["ACCEPT", "REJECT", "INCONCLUSIVE", "INSUFFICIENT_DATA"]


@dataclass
class ResearchVerdict:
    """The outcome of running :func:`decide` against one candidate's results.

    Attributes:
        decision: The final call. Never inferred elsewhere -- this is the one
            place a piece of research code is allowed to produce it.
        reasons: Human-readable justification, one entry per gate that drove
            the decision (usually one entry, but a REJECT can cite more than
            one failing gate at once).
        metrics: The metrics payload this verdict was computed from
            (typically ``TradingMetrics.as_dict()``), stored so the verdict
            is self-contained without a live recomputation.
        checklist: One boolean per named gate
            (``sufficient_trades``, ``sufficient_regimes``,
            ``charter_metrics``, ``walk_forward``, ``cost_slippage_robust``,
            ``monte_carlo``). ``False`` covers both "failed" and "not
            evaluated" -- the ``reasons`` text is what distinguishes them.
    """

    decision: Decision
    reasons: list[str]
    metrics: dict[str, Any]
    checklist: dict[str, bool]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "metrics": self.metrics,
            "checklist": dict(self.checklist),
        }


def _empty_checklist() -> dict[str, bool]:
    return {
        "sufficient_trades": False,
        "sufficient_regimes": False,
        "charter_metrics": False,
        "walk_forward": False,
        "cost_slippage_robust": False,
        "monte_carlo": False,
    }


def decide(
    *,
    trade_count: int,
    metrics: TradingMetrics,
    oos_verdict: dict[str, Any] | None = None,
    robustness_flagged_fragile: bool | None = None,
    monte_carlo_gates_passed: bool | None = None,
    regime_count_covered: int | None = None,
    min_trade_count: int = 100,
    min_regime_count: int = 2,
) -> ResearchVerdict:
    """Decide ACCEPT / REJECT / INCONCLUSIVE / INSUFFICIENT_DATA from fixed rules.

    See the module docstring for the exact priority order this follows and
    why the order matters. Evaluation stops and returns at the first gate
    that produces a non-passing outcome, except for gate 7 (missing checks),
    which collects every missing check before returning so the caller does
    not have to re-run this repeatedly to discover each one.

    Args:
        trade_count: Number of realized trades the metrics were computed
            from. Passed explicitly (rather than read off ``metrics`` only)
            so a caller cannot accidentally evaluate a decision against a
            metrics object whose trade_count does not match the sample this
            decision is actually about.
        metrics: Trading metrics for the candidate.
        oos_verdict: A walk-forward result's verdict dict, expected to carry
            a ``"passed"`` key when present. ``None`` means walk-forward was
            never run.
        robustness_flagged_fragile: Whether a cost/slippage stress test
            flagged this candidate as fragile. ``None`` means that stress
            test was never run.
        monte_carlo_gates_passed: The ``"all_gates_passed"`` outcome of a
            Monte Carlo run. ``None`` means Monte Carlo was never run.
        regime_count_covered: How many distinct market regimes the trades
            spanned. ``None`` means regime coverage was never assessed --
            in which case this gate is skipped (not treated as a failure),
            since not every experiment tracks regimes.
        min_trade_count: Charter's statistical-significance floor.
        min_regime_count: Minimum distinct regimes required to generalize.

    Returns:
        The verdict, with reasons and a full checklist.
    """
    checklist = _empty_checklist()

    # 1. Sample size.
    if trade_count < min_trade_count:
        return ResearchVerdict(
            decision="INSUFFICIENT_DATA",
            reasons=[
                f"Only {trade_count} trades recorded, below the {min_trade_count}-trade "
                "floor required before any conclusion can be drawn."
            ],
            metrics=metrics.as_dict(),
            checklist=checklist,
        )
    checklist["sufficient_trades"] = True

    # 2. Regime coverage.
    if regime_count_covered is not None and regime_count_covered < min_regime_count:
        return ResearchVerdict(
            decision="INSUFFICIENT_DATA",
            reasons=[
                f"Trades span only {regime_count_covered} distinct market regime(s), below "
                f"the {min_regime_count} required to support a general claim of edge rather "
                "than a result specific to one regime."
            ],
            metrics=metrics.as_dict(),
            checklist=checklist,
        )
    if regime_count_covered is not None:
        checklist["sufficient_regimes"] = True

    # 3. Charter deployment-gate metrics (same thresholds as the leaderboard).
    metric_failures: list[str] = []
    if metrics.profit_factor is None or metrics.profit_factor < MIN_PROFIT_FACTOR:
        pf = "undefined (no losing trades)" if metrics.profit_factor is None else f"{metrics.profit_factor:.2f}"
        metric_failures.append(f"Profit factor {pf} is below the {MIN_PROFIT_FACTOR} charter gate.")
    if metrics.win_rate < MIN_WIN_RATE:
        metric_failures.append(f"Win rate {metrics.win_rate:.1%} is below the {MIN_WIN_RATE:.0%} charter gate.")
    if metrics.sharpe is None or metrics.sharpe < MIN_SHARPE:
        sharpe = "undefined" if metrics.sharpe is None else f"{metrics.sharpe:.2f}"
        metric_failures.append(f"Sharpe {sharpe} is below the {MIN_SHARPE} charter gate.")

    if metric_failures:
        return ResearchVerdict(
            decision="REJECT",
            reasons=metric_failures,
            metrics=metrics.as_dict(),
            checklist=checklist,
        )
    checklist["charter_metrics"] = True

    # 4. Out-of-sample / walk-forward.
    if oos_verdict is not None and not oos_verdict.get("passed", False):
        return ResearchVerdict(
            decision="REJECT",
            reasons=[
                "Walk-forward (out-of-sample) validation failed: "
                f"{oos_verdict.get('reason', 'no reason recorded')}."
            ],
            metrics=metrics.as_dict(),
            checklist=checklist,
        )

    # 5. Cost/slippage robustness.
    if robustness_flagged_fragile is True:
        return ResearchVerdict(
            decision="REJECT",
            reasons=["Flagged fragile under cost/slippage stress testing."],
            metrics=metrics.as_dict(),
            checklist=checklist,
        )

    # 6. Monte Carlo risk-of-ruin / drawdown gates.
    if monte_carlo_gates_passed is False:
        return ResearchVerdict(
            decision="REJECT",
            reasons=["Monte Carlo risk-of-ruin/drawdown gate(s) failed."],
            metrics=metrics.as_dict(),
            checklist=checklist,
        )

    # 7. Anything not yet evaluated blocks ACCEPT, even though nothing failed.
    missing: list[str] = []
    if oos_verdict is None:
        missing.append("walk-forward validation was never run")
    else:
        checklist["walk_forward"] = True
    if robustness_flagged_fragile is None:
        missing.append("cost/slippage robustness stress test was never run")
    else:
        checklist["cost_slippage_robust"] = True
    if monte_carlo_gates_passed is None:
        missing.append("Monte Carlo simulation was never run")
    else:
        checklist["monte_carlo"] = True

    if missing:
        return ResearchVerdict(
            decision="INCONCLUSIVE",
            reasons=[
                "Charter metrics pass, but this cannot be ACCEPTed because: " + "; ".join(missing) + "."
            ],
            metrics=metrics.as_dict(),
            checklist=checklist,
        )

    # 8. Every gate present and passed.
    return ResearchVerdict(
        decision="ACCEPT",
        reasons=[
            "All gates passed: sufficient trades, sufficient regime coverage (if assessed), "
            "charter metrics, walk-forward, cost/slippage robustness, and Monte Carlo."
        ],
        metrics=metrics.as_dict(),
        checklist=checklist,
    )
