"""Seven ground-truth scenario builders for the synthetic validation harness.

Each function returns ``(config, expected_verdict, ground_truth)``. The
config is fed to :class:`research.validation.synthetic_generator.SyntheticHistoricalProvider`
and then driven through the real pipeline by
:mod:`research.validation.runner`; ``expected_verdict`` is what
``research.decision.decide`` *should* say given how the data was built;
``ground_truth`` documents exactly what was planted and why, including the
cost-floor arithmetic for the two cost-sensitive scenarios (2 and 4), so the
"smaller/larger than costs" claims are computed, not asserted.
"""

from __future__ import annotations

from typing import Any

from backtest.costs import CostModel
from research.validation.synthetic_generator import SyntheticScenarioConfig

#: Shared marginal distribution (volatility, IV dynamics) across scenarios 1
#: and 2, so scenario 2 is a genuine "same noise, no signal" control rather
#: than a differently-shaped dataset that happens to also lack a plant.
_BASE_KWARGS: dict[str, Any] = dict(
    n_sessions=260,
    bars_per_session=75,
    underlying_start_price=20000.0,
    drift_annualized=0.08,
    volatility_annualized=0.18,
    vol_clustering=0.4,
    regime_switch_prob=0.03,
    iv_base=0.16,
    iv_term_structure_slope=0.02,
    iv_vol_response=0.4,
)

#: The lot size / quantity assumption every scenario's runner uses when
#: pricing round-trip costs (see research.validation.runner). Kept at 1 (as
#: research.cli.run_backtest's own default does) so the engine's risk-based
#: position sizing (backtest.broker_simulator.calculate_position_size) never
#: rounds a real, affordable position down to zero lots purely because of a
#: lot-size granularity mismatch unrelated to what this harness is testing.
ASSUMED_LOT_SIZE = 1
#: Representative option premium (rupees) used only for the cost-floor
#: arithmetic below -- the generator's actual premiums vary bar to bar; this
#: is a documented order-of-magnitude reference price, not a hidden pipeline
#: input.
_REFERENCE_PREMIUM = 120.0


def _cost_floor_pct() -> float:
    """Round-trip cost as a percentage of premium, for a long option trade.

    Computed from the real, unmodified :class:`backtest.costs.CostModel`
    (never hand-typed), at :data:`ASSUMED_LOT_SIZE` quantity and
    :data:`_REFERENCE_PREMIUM` price. This is the floor an edge's *option
    premium* return-on-capital must clear to survive costs at all.
    """
    model = CostModel()
    breakeven_move = model.breakeven_move(
        "OPTION", price=_REFERENCE_PREMIUM, quantity=ASSUMED_LOT_SIZE, direction="LONG"
    )
    return round(breakeven_move / _REFERENCE_PREMIUM * 100.0, 4)


def build_planted_edge_scenario(seed: int) -> tuple[SyntheticScenarioConfig, str, dict[str, Any]]:
    """Scenario 1: a real, cost-surviving edge tied to a per-session signal.

    ``planted_effect_size=2.0`` means: sessions where the planted signal is
    +1 get an extra +2% (in aggregate, over the session) underlying drift;
    sessions with signal -1 get -2%. For a short-dated, near-ATM option
    (delta ~0.4-0.6), a 2% underlying move translates to a large multiple of
    the option's own premium (see the generator's own sanity check in
    ``tests/research/test_synthetic_generator.py`` for the empirical
    correct-direction-vs-economics relationship) -- far above the
    :func:`_cost_floor_pct` round-trip cost floor computed below.
    """
    cost_floor_pct = _cost_floor_pct()
    config = SyntheticScenarioConfig(
        seed=seed,
        planted_feature="session_signal",
        planted_effect_size=2.0,
        **_BASE_KWARGS,
    )
    ground_truth = {
        "description": (
            "A per-session +1/-1 signal is planted; +1 sessions get +2% extra "
            "underlying drift over the session, -1 sessions get -2%. Trading "
            "only the +1 (high) bucket with a long CE should show a real, "
            "cost-surviving edge relative to the -1 (low) bucket or to trading "
            "every session unconditionally. Calibrated empirically (see "
            "research/validation/scenarios.py's development notes) to clear "
            "every charter gate (profit factor, win rate, Sharpe) while still "
            "producing some losing trades -- a deterministic 100%-win-rate "
            "plant would fail the profit-factor gate, which treats an "
            "undefined profit factor (no losing trades at all) as a failure, "
            "not a pass."
        ),
        "planted_effect_size_underlying_pct": 2.0,
        "cost_floor_pct_of_premium": cost_floor_pct,
        "cost_floor_arithmetic": (
            f"CostModel().breakeven_move('OPTION', price={_REFERENCE_PREMIUM}, "
            f"quantity={ASSUMED_LOT_SIZE}, direction='LONG') / {_REFERENCE_PREMIUM} * 100 "
            f"= {cost_floor_pct}% of premium round-trip. The planted 2% underlying move "
            "is expected to move the option premium by well over an order of "
            "magnitude more than this floor, given short-dated near-ATM gamma."
        ),
        "bucket": "high (+1)",
    }
    return config, "ACCEPT", ground_truth


def build_pure_noise_scenario(seed: int) -> tuple[SyntheticScenarioConfig, str, dict[str, Any]]:
    """Scenario 2: identical marginals to scenario 1, but no planted feature.

    Expected REJECT or INCONCLUSIVE are both acceptable null outcomes: with
    no real edge, the charter's metrics gate (profit factor/win rate/Sharpe)
    should fail outright (REJECT), *or*, if noise happens to clear those
    gates on this particular seed, the walk-forward/cost-stress/Monte Carlo
    checks (never run here, since this harness does not fabricate a passing
    OOS result for noise) leave the verdict INCONCLUSIVE rather than ACCEPT.
    Either is a correct "no real edge" answer; ACCEPT is the only wrong one.
    """
    config = SyntheticScenarioConfig(
        seed=seed,
        planted_feature=None,
        planted_effect_size=None,
        **_BASE_KWARGS,
    )
    ground_truth = {
        "description": (
            "Same volatility/IV dynamics as scenario 1, no planted feature. "
            "There is no real, exploitable relationship between any "
            "observable session-level quantity and net P&L."
        ),
        "planted_effect_size_underlying_pct": None,
    }
    return config, ["REJECT", "INCONCLUSIVE"], ground_truth


def build_in_sample_only_edge_scenario(seed: int) -> tuple[SyntheticScenarioConfig, str, dict[str, Any]]:
    """Scenario 3: the planted edge only exists in the first half of each session's bars.

    A strategy (or a naive researcher) fit on the whole sample would see
    the edge in aggregate return but it would not generalize out of sample
    within the session -- the second half of each session carries none of it.
    Modeled here as ``planted_effect_window=(0, bars_per_session // 2)``.
    """
    bars_per_session = _BASE_KWARGS["bars_per_session"]
    window = (0, bars_per_session // 2)
    config = SyntheticScenarioConfig(
        seed=seed,
        planted_feature="session_signal",
        planted_effect_size=2.0,
        planted_effect_window=window,
        **_BASE_KWARGS,
    )
    ground_truth = {
        "description": (
            f"The planted +-2% signal-tied drift is confined to bars {window} of "
            f"each {bars_per_session}-bar session only; the second half carries no "
            "edge. A BuyAndHold trade spans the whole session, so this is a "
            "harder case than a clean in-sample/out-of-sample split -- the edge "
            "is diluted, not simply absent, in the full-session P&L, which is "
            "exactly the fragile-signal pattern real strategies produce when "
            "an effect decays intra-trade."
        ),
        "planted_effect_window_bars": window,
    }
    return config, ["REJECT", "INCONCLUSIVE"], ground_truth


def build_cost_killed_edge_scenario(seed: int) -> tuple[SyntheticScenarioConfig, str, dict[str, Any]]:
    """Scenario 4: THE most important scenario in this phase.

    A genuine gross directional effect exists (the planted signal really
    does bias the underlying's drift), but sized far below the round-trip
    cost floor computed from the real, unmodified :class:`backtest.costs.CostModel`.
    If this scenario ACCEPTs, the pipeline is systematically over-crediting
    an edge that cannot actually be traded profitably -- the single most
    dangerous failure mode this whole harness exists to catch.
    """
    cost_floor_pct = _cost_floor_pct()
    # 0.05% underlying drift is roughly 100x smaller than scenario 1's 6%,
    # and -- unlike scenario 1's clearly-above-floor sizing -- is chosen to
    # sit at a fraction of the round-trip cost floor even after accounting
    # for short-dated option leverage, so that after realistic per-trade
    # slippage and statutory costs the gross edge cannot survive.
    tiny_effect_pct = 0.05
    config = SyntheticScenarioConfig(
        seed=seed,
        planted_feature="session_signal",
        planted_effect_size=tiny_effect_pct,
        **_BASE_KWARGS,
    )
    ground_truth = {
        "description": (
            f"A real +-{tiny_effect_pct}% underlying drift is planted (verifiably "
            "nonzero, unlike scenario 2's pure noise), but it is small enough "
            "that round-trip costs should consume it. This is the scenario "
            "the phase brief calls out by name: a pipeline that ACCEPTs here "
            "is unsafe to trust on any real edge-sizing question."
        ),
        "planted_effect_size_underlying_pct": tiny_effect_pct,
        "cost_floor_pct_of_premium": cost_floor_pct,
        "cost_floor_arithmetic": (
            f"CostModel().breakeven_move('OPTION', price={_REFERENCE_PREMIUM}, "
            f"quantity={ASSUMED_LOT_SIZE}, direction='LONG') / {_REFERENCE_PREMIUM} * 100 "
            f"= {cost_floor_pct}% of premium round-trip; the {tiny_effect_pct}% planted "
            "underlying move is deliberately sized small relative to typical "
            "intraday noise so its option-premium effect is comparable to, "
            "not many multiples of, this floor."
        ),
    }
    return config, "REJECT", ground_truth


def build_regime_conditional_edge_scenario(seed: int) -> tuple[SyntheticScenarioConfig, str, dict[str, Any]]:
    """Scenario 5: the edge exists only in one market regime.

    Unconditional evaluation (pooling every session regardless of regime)
    should not ACCEPT, since the edge washes out against sessions where it
    is absent. Per the phase brief, this harness does not add a new
    regime-conditional acceptance path to the pipeline -- it only checks,
    as an *extra* diagnostic beyond the top-line verdict, whether
    ``research.regime.engine.RegimeDetector`` run against the generated
    underlying path actually recovers the regime segmentation used to plant
    the effect (see ``research/validation/runner.py``'s
    ``regime_breakdown`` in the result).
    """
    config = SyntheticScenarioConfig(
        seed=seed,
        planted_feature="session_signal",
        planted_effect_size=2.0,
        planted_effect_regime="TREND_UP",
        **_BASE_KWARGS,
    )
    ground_truth = {
        "description": (
            "The generator confines the +-2% signal-tied drift to sessions "
            "whose *generative* regime (a real, per-bar trending-drift "
            "component the same magnitude and mechanism research.regime.engine "
            ".RegimeDetector is built to detect from price structure) is "
            "TREND_UP; every other session gets none of the plant. Trading the "
            "high bucket unconditionally on regime (the same strategy as "
            "scenario 1) pools TREND_UP sessions carrying the edge together "
            "with other-regime sessions carrying none of it, and should not "
            "clear the charter gates the way scenario 1's every-session plant "
            "does. There is no regime-conditional acceptance path in "
            "research.decision.decide today -- this scenario's "
            "regime_breakdown diagnostic (built from a real RegimeDetector run "
            "over each session's own bars) exists only to surface whether the "
            "effect is visible when sliced by regime, not to change the "
            "top-line verdict."
        ),
        "planted_effect_regime": "TREND_UP",
    }
    return config, ["REJECT", "INCONCLUSIVE"], ground_truth


def build_thin_data_scenario(seed: int) -> tuple[SyntheticScenarioConfig, str, dict[str, Any]]:
    """Scenario 6: scenario 1's real edge, truncated below the charter's trade-count floor."""
    kwargs = dict(_BASE_KWARGS)
    kwargs["n_sessions"] = 12  # well under decide()'s min_trade_count=100
    config = SyntheticScenarioConfig(
        seed=seed,
        planted_feature="session_signal",
        planted_effect_size=2.0,
        **kwargs,
    )
    ground_truth = {
        "description": (
            "Identical edge construction to scenario 1, but only 12 sessions "
            "(12 pooled trades) instead of 150 -- below research.decision.decide's "
            "min_trade_count=100 floor. The edge itself is real; the sample is "
            "too small to say so."
        ),
        "n_sessions": kwargs["n_sessions"],
    }
    return config, "INSUFFICIENT_DATA", ground_truth


def build_lookahead_trap_scenario(seed: int) -> tuple[SyntheticScenarioConfig, str, dict[str, Any]]:
    """Scenario 7: attempt to construct a feature that reads future data.

    ``research.features.provenance.FeatureSpec.__post_init__`` already
    raises ``ValueError`` for any spec declaring ``uses_future_data=True`` --
    a hard invariant enforced before this harness ever runs, not something
    this scenario's data needs to trigger dynamically. The "expected
    verdict" here is therefore about the *attempt*, not about
    ``research.decision.decide``: registering a lookahead feature through
    the sanctioned path must be structurally impossible, and this is
    verified directly in ``tests/research/test_validation_scenarios.py``
    (not by running a full backtest -- there is nothing else in this dataset
    that requires a full pipeline run for this particular check).
    """
    config = SyntheticScenarioConfig(
        seed=seed,
        lookahead_leak=True,
        **_BASE_KWARGS,
    )
    ground_truth = {
        "description": (
            "The dataset itself is unremarkable (same marginals as scenario 2); "
            "what this scenario tests is whether the real feature-provenance "
            "machinery (research.features.provenance.FeatureSpec) refuses to "
            "let a future-reading feature be registered at all, and separately "
            "whether any other pipeline path computes an ad hoc feature outside "
            "that machinery without the same protection."
        ),
    }
    return config, "STRUCTURALLY_IMPOSSIBLE", ground_truth


#: name -> builder, in the order the runner and CLI iterate them.
SCENARIO_BUILDERS: dict[str, Any] = {
    "1_planted_edge": build_planted_edge_scenario,
    "2_pure_noise": build_pure_noise_scenario,
    "3_in_sample_only_edge": build_in_sample_only_edge_scenario,
    "4_cost_killed_edge": build_cost_killed_edge_scenario,
    "5_regime_conditional_edge": build_regime_conditional_edge_scenario,
    "6_thin_data": build_thin_data_scenario,
    "7_lookahead_trap": build_lookahead_trap_scenario,
}
