"""Cost-aware ML targets: labeling a trade's economics, not its direction.

The charter for this phase is explicit: the target must not be "did the
underlying move the right way," because a correct directional call and a
profitable option trade are different questions. Theta, IV contraction, the
bid/ask crossed on entry and exit, and brokerage/STT/GST can all turn a
correct directional read into a losing trade — see
``tests/research/test_option_economics_vs_direction.py`` for a worked example.

Labels here are therefore built from :class:`backtest.types.Trade`, which
already carries ``net_pnl`` (after the real cost model, including slippage)
rather than from the underlying's price change. A trade that made money on
paper before costs but not after is labeled on the after-costs number,
because that is the number that is actually real.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backtest.types import Trade

Label = Literal["CALL_FAVORABLE", "PUT_FAVORABLE", "NEUTRAL"]


@dataclass
class LabeledExample:
    """One training example: measured features plus the realized, cost-net outcome.

    Attributes:
        features: The feature vector at entry time. This module does not
            compute it — it is supplied by the caller, from
            :mod:`research.features.engine` or elsewhere — because a target
            and a feature set are independent concerns and must stay
            separately versioned (see ``research/data/models.py``'s
            ``DatasetVersion.feature_version``).
        option_type: ``CE`` or ``PE`` — which side of the market this
            structure was a bet on.
        gross_pnl: P&L before any charge.
        costs: Statutory charges only (brokerage, STT, exchange charges, GST,
            SEBI fee, stamp duty). Excludes slippage, which is its own field,
            so ``gross_pnl - costs - slippage == net_pnl`` without double
            counting.
        slippage: Modelled slippage, separate from statutory costs.
        net_pnl: What actually happened, after every charge.
        net_return_pct: ``net_pnl`` as a percentage of capital deployed
            (regression target).
        label: Classification target. See :func:`label_for`.
        underlying_direction_correct: Whether the underlying moved the way
            the structure's direction implied, independent of whether the
            trade was profitable — the explicit separation Phase 2 requires.
    """

    features: dict[str, float]
    option_type: Literal["CE", "PE"]
    gross_pnl: float
    costs: float
    slippage: float
    net_pnl: float
    net_return_pct: float
    label: Label
    underlying_direction_correct: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "features": self.features,
            "option_type": self.option_type,
            "gross_pnl": self.gross_pnl,
            "costs": self.costs,
            "slippage": self.slippage,
            "net_pnl": self.net_pnl,
            "net_return_pct": self.net_return_pct,
            "label": self.label,
            "underlying_direction_correct": self.underlying_direction_correct,
        }


def label_for(
    net_pnl: float,
    option_type: Literal["CE", "PE"],
    *,
    capital_deployed: float,
    favorable_return_pct: float = 5.0,
) -> Label:
    """Classify a realized, cost-net outcome.

    A merely-positive net P&L is not automatically "favorable" — the
    threshold exists precisely so the label distinguishes ``₹30 net on a
    ₹1,000 outlay`` from ``₹1,830 net on the same outlay`` (the Phase 2 spec's
    own example). Below the threshold, and above zero, the honest label is
    still ``NEUTRAL``: the trade neither demonstrated an edge nor a clear
    failure, and a classifier trained to call it "favorable" would be
    learning noise.

    Args:
        net_pnl: Realized P&L after every cost.
        option_type: Which side of the market the structure bet on. A
            profitable CE trade is CALL_FAVORABLE; a profitable PE trade is
            PUT_FAVORABLE. Losses and marginal wins are NEUTRAL regardless of
            side, since "unfavorable" is not informative about which
            direction *would* have worked.
        capital_deployed: Premium or margin at risk, for the percentage basis.
        favorable_return_pct: Minimum net return, as a percentage of capital
            deployed, to earn a favorable label rather than NEUTRAL.

    Returns:
        The label.
    """
    if capital_deployed <= 0:
        return "NEUTRAL"
    return_pct = net_pnl / capital_deployed * 100.0
    if return_pct < favorable_return_pct:
        return "NEUTRAL"
    return "CALL_FAVORABLE" if option_type == "CE" else "PUT_FAVORABLE"


def example_from_trade(
    trade: Trade,
    features: dict[str, float],
    *,
    option_type: Literal["CE", "PE"],
    favorable_return_pct: float = 5.0,
    underlying_direction_correct: bool | None = None,
) -> LabeledExample:
    """Build a labeled example from a completed, cost-net trade.

    Args:
        trade: A closed trade from a real backtest run (never a hypothetical
            or estimated one — see ``research/backtest/options_backtest.py``).
        features: Feature vector measured at entry.
        option_type: Which side of the market this structure bet on.
        favorable_return_pct: Passed to :func:`label_for`.
        underlying_direction_correct: Optional, explicit record of whether the
            underlying's own move agreed with the structure's directional
            bias — kept separate from profitability by design.

    Returns:
        The labeled example.
    """
    capital_deployed = abs(trade.entry_price * trade.quantity)
    slippage = getattr(trade.costs, "slippage", 0.0)
    # `.statutory` excludes slippage (brokerage/STT/exchange/GST/SEBI/stamp
    # only), kept separate from `slippage` here so gross_pnl - costs -
    # slippage reproduces net_pnl without double-counting either figure.
    costs_total = (
        trade.costs.statutory if hasattr(trade.costs, "statutory") else float(trade.costs)
    )

    return LabeledExample(
        features=dict(features),
        option_type=option_type,
        gross_pnl=trade.gross_pnl,
        costs=costs_total,
        slippage=slippage,
        net_pnl=trade.net_pnl,
        net_return_pct=round(trade.net_pnl / capital_deployed * 100.0, 4)
        if capital_deployed > 0
        else 0.0,
        label=label_for(
            trade.net_pnl,
            option_type,
            capital_deployed=capital_deployed,
            favorable_return_pct=favorable_return_pct,
        ),
        underlying_direction_correct=underlying_direction_correct,
    )


def label_distribution(examples: list[LabeledExample]) -> dict[str, int]:
    """Count examples per label, for a quick class-balance check before training."""
    counts: dict[str, int] = {}
    for example in examples:
        counts[example.label] = counts.get(example.label, 0) + 1
    return counts
