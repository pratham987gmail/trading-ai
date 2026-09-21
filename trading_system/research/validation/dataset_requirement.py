"""How much historical data this research pipeline actually needs (Phase 3.5).

``research/data/upstox/readiness.py`` already gates on ``min_trading_days``,
``min_expiries``, ``min_contracts`` -- but its own docstring calls those
"deliberately conservative defaults," chosen by analogy to other floors in
the codebase rather than derived. This module does the derivation: it starts
from the charter thresholds this project has already committed to
(``research/experiments/leaderboard.py``, ``backtest/walk_forward.py``,
``research/decision.py``) and from the real walk-forward window mechanics
(``research/backtest/options_walk_forward.py``), and works forward to a
concrete bar/day/storage requirement -- plus, separately, a statistical
power analysis of whether the charter's own 100-trade floor is even enough
to detect a real edge against realistic option-P&L noise.

**Placement.** This lives under ``research/validation/`` (alongside
``synthetic_generator.py``, which does the companion job of producing
synthetic data to validate the pipeline's statistics) rather than under
``research/data/`` (which is about *sourcing and storing* real data, not
*sizing* the requirement for it). ``research/data/upstox/readiness.py``
stays untouched; this module produces a :class:`DatasetRequirement` that
could later be fed into that function's ``min_trading_days`` /
``min_expiries`` / ``min_contracts`` keyword arguments, but does not modify
it, per this phase's read-only-reuse constraint.

**Every numeric floor pulled from other modules is imported, not
retyped** -- ``MIN_TRADES_FOR_STATUS``, ``MIN_OUT_OF_SAMPLE_TRADES``,
``OptionsWalkForwardRequest``'s ``train_bars``/``test_bars`` defaults,
``decide()``'s ``min_regime_count`` default, ``RegimeDetector``'s
``confirmation_bars`` default, and the real 375-minute NSE session length
via ``research.data.upstox.calendar.expected_bar_count`` -- so this module
breaks loudly (via its own test) if any of those change without this
module's understanding of them being updated too.

**What is NOT importable and had to be reasoned instead** (see
``unverified_assumptions``): the dispersion of real per-trade option P&L
(the project's real database is empty, so nothing here can be measured --
Part A's synthetic scenarios, Part B's cost audit, and an eventual Upstox
backfill are what will eventually produce a real number), how many strikes
around ATM a strategy needs recorded, and how many expiries should be
tracked simultaneously per trading day. Every one of these is labeled
REASONED or UNVERIFIED at the point it is introduced, per this phase's
explicit instruction not to dress up a guess as a fact.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from backtest.costs import DEFAULT_COST_MODEL
from backtest.walk_forward import MIN_OUT_OF_SAMPLE_TRADES
from research.backtest.options_walk_forward import OptionsWalkForwardRequest
from research.data.upstox.calendar import expected_bar_count
from research.decision import decide
from research.experiments.leaderboard import MIN_TRADES_FOR_STATUS
from research.regime.engine import ALL_REGIMES, RegimeDetector

# ---------------------------------------------------------------------------
# VERIFIED / COMPUTED constants -- pulled live from the modules that own them.
# ---------------------------------------------------------------------------

#: Walk-forward train/test window sizes this project actually uses for an
#: options structure (``research/backtest/options_walk_forward.py``'s own
#: dataclass defaults), read via the dataclass field rather than retyped.
TRAIN_BARS: int = OptionsWalkForwardRequest.__dataclass_fields__["train_bars"].default
TEST_BARS: int = OptionsWalkForwardRequest.__dataclass_fields__["test_bars"].default
#: ``backtest.walk_forward.generate_windows`` treats ``step_bars=None`` as
#: "use test_bars" (non-overlapping test slices) -- and
#: ``OptionsWalkForwardRequest.step_bars`` defaults to ``None`` -- so the
#: real effective step is ``TEST_BARS``, not a separately-chosen number.
STEP_BARS: int = TEST_BARS

#: The charter's own trade-count floor for drawing any conclusion at all
#: (``research/experiments/leaderboard.py``), reused, not retyped.
CHARTER_MIN_TRADES: int = MIN_TRADES_FOR_STATUS

#: The absolute out-of-sample trade floor a walk-forward verdict requires
#: before it can pass (``backtest/walk_forward.py``).
MIN_OOS_TRADES: int = MIN_OUT_OF_SAMPLE_TRADES

#: ``research.decision.decide``'s own default for how many distinct regimes
#: a result must span before it can support a general claim of edge, read
#: off the live function signature rather than copied.
MIN_REGIME_COUNT: int = inspect.signature(decide).parameters["min_regime_count"].default

#: ``RegimeDetector``'s own default hysteresis window, read off the live
#: constructor signature.
REGIME_CONFIRMATION_BARS: int = inspect.signature(RegimeDetector.__init__).parameters[
    "confirmation_bars"
].default

#: The number of distinct labels ``RegimeDetector`` can ever assign
#: (``research/regime/engine.py``'s ``ALL_REGIMES``), imported rather than
#: hardcoded as "7".
N_REGIME_LABELS: int = len(ALL_REGIMES)

#: A real NSE trading session is 09:15-15:30 IST = 375 minutes
#: (``core.market_hours.MARKET_OPEN``/``MARKET_CLOSE``, re-exported through
#: ``research.data.upstox.calendar``). Computed via the project's own
#: ``expected_bar_count`` helper rather than a hardcoded "375" / "75", so
#: this stays correct if the session definition ever changes. The date
#: argument is inert (a full session has a fixed length regardless of
#: date -- see that function's own docstring), so any trading date works.
_ANY_TRADING_DATE = date(2026, 1, 5)
BARS_PER_SESSION_1MIN: int = expected_bar_count(_ANY_TRADING_DATE, "minutes", 1) or 375
BARS_PER_SESSION_5MIN: int = expected_bar_count(_ANY_TRADING_DATE, "minutes", 5) or 75

# ---------------------------------------------------------------------------
# STANDARD, CITABLE approximations -- not fabricated for this module, but
# also not derived from project code, so kept clearly separate from the
# block above.
# ---------------------------------------------------------------------------

#: The standard rule-of-thumb used across quantitative finance for
#: converting a trading-day count to roughly a calendar year (~52 weeks x 5
#: sessions, less ~10-12 NSE holidays). Cited as a well-known approximation,
#: not measured from this project's own holiday calendar.
TRADING_DAYS_PER_YEAR_APPROX: int = 250

#: Standard two-sided-alpha / power defaults for a sample-size calculation.
#: 95% confidence (two-sided) and 80% power are the conventional defaults
#: in applied statistics (Cohen's own convention for power analysis, and
#: the pairing used by every common sample-size calculator), chosen here
#: for the same reason: they are the values a reviewer expects to see
#: without special justification, not a number picked to produce a
#: particular answer.
Z_ALPHA_HALF: float = 1.959963985  # two-sided 95% confidence
Z_BETA: float = 0.841621234  # 80% power

# ---------------------------------------------------------------------------
# REASONED WORKING ASSUMPTIONS -- explicitly labeled, not sourced facts.
# ---------------------------------------------------------------------------

#: Illustrative premium example used to price a round-trip's cost floor:
#: a NIFTY-style option at ~150 premium, one lot. The lot size figure is
#: illustrative (real lot sizes are revised by SEBI/exchange circular and
#: are not the subject of this module); it only needs to be a realistic
#: order of magnitude for turnover-based statutory costs to compute
#: sensibly.
_ILLUSTRATIVE_PREMIUM = 150.0
_ILLUSTRATIVE_LOT_QTY = 65

#: REASONED, not measured: the per-trade net-return dispersion (as a
#: percentage of option premium) used for the power analysis below. The
#: project's real trade database is empty, so this cannot be measured
#: today. It is instead reasoned from the illustrative synthetic premium
#: path in ``tests/research/test_option_economics_vs_direction.py``
#: (150 -> 140 -> 128 -> 118 -> 108, i.e. a ~28% premium decline over four
#: bars, and the module's own docstring frames 30-50% short-window premium
#: swings as the scale of legitimate option-P&L volatility). 30% is chosen
#: as a representative *single-trade* dispersion figure -- roughly the
#: lower end of that illustrative range, since a single round-trip trade
#: should see somewhat less variance than a compounded multi-bar
#: demonstration path. This number MUST eventually be replaced by a
#: measurement from real recorded trades once the Upstox backfill and a
#: real backtest produce some.
ASSUMED_SIGMA_PCT_OF_PREMIUM: float = 30.0

#: Candidate "minimum meaningful effect size" values to run the power
#: calculation against, expressed as mean net return per trade as a
#: percentage of option premium. Deliberately a range rather than one
#: number, because the true answer is a curve (see the module docstring
#: and the report this feeds).
EFFECT_SIZES_PCT_OF_PREMIUM: list[float] = [0.5, 1.0, 2.0, 5.0, 10.0]

#: REASONED: the multiplier applied to the computed cost floor to define
#: "a modest edge worth having" for the headline recommendation. 2x is a
#: common, unremarkable working choice (an edge that merely equals its own
#: cost floor is not worth trading; one that is several multiples of it
#: clearly is -- 2x is a conservative middle point between those), not a
#: sourced constant.
_RECOMMENDED_EFFECT_SIZE_MULTIPLE = 2.0

#: REASONED: how many distinct expiries a realistic backfill would track
#: *simultaneously* on any given trading day (e.g. the current week, next
#: week, and the current month's expiry) -- not the total number of
#: distinct expiries that occur across a whole year. Multiplying a full
#: year's trading days by a full year's expiry count would enormously
#: overcount, since any one expiry contract only trades during its own
#: listed life. This is a reasoned working figure for a practical
#: multi-expiry backfill, not derived from project code (no existing
#: module states how many expiries a backfill should track at once).
AVG_LISTED_EXPIRIES_PER_DAY: int = 3

#: REASONED: ATM +/- 10 strikes (21 total per expiry per side). No strike
#: range is hardcoded anywhere in this project's strategy generators today
#: (``research/strategies/option_selection.py`` implements ATM / ATM+1 /
#: ATM-1 / DELTA *selection methods*, not a fixed scan width), so this is
#: an explicitly reasoned working range, not a sourced fact -- chosen
#: because it is wide enough to cover ATM+1/ATM-1 selection plus a
#: reasonable margin for strategies that scan further OTM/ITM, without
#: recording the entire chain.
STRIKES_PER_EXPIRY: int = 21

#: The project's own stated scope (``UPSTOX_INTEGRATION_GUIDE.md``,
#: ``core/upstox_instruments.py``'s NSE_INDEX/BSE_INDEX handling).
INSTRUMENTS: list[str] = ["NIFTY", "BANKNIFTY", "SENSEX"]

#: REASONED: bytes per stored bar row, including index overhead, for
#: ``research/data/upstox/store.py``'s ``bars`` table. That table has 22
#: columns (mostly TEXT: provider, instrument_key, underlying, expiry,
#: option_type, exchange, segment, interval_unit, timestamp,
#: source_timestamp, ingested_at, dataset_version, data_source_kind; plus
#: 8-byte REALs for strike/OHLC/volume/OI and an INTEGER interval_value).
#: A rough per-row estimate: identifier/text columns average ~15-25 bytes
#: each (~150 bytes across ~10 such columns), 5 REAL price/size columns at
#: 8 bytes (~40 bytes), plus SQLite's own per-column varint/length
#: overhead (~1-2 bytes/column, ~30 bytes total) sums to roughly 220 bytes
#: of raw row payload; the two declared indexes
#: (``idx_bars_lookup``, ``idx_bars_instrument``) add roughly another
#: 30-40% on top for their own B-tree entries. ~350 bytes/row (raw + both
#: indexes) is used below. This is an estimate from the schema's own
#: column list, not a measurement against a populated database (the real
#: store is empty), and should be revisited once real bars exist to
#: measure an actual on-disk bytes/row figure.
ESTIMATED_BYTES_PER_ROW: float = 350.0

#: REASONED minimum number of out-of-sample walk-forward windows before
#: ``efficiency_ratio``/``parameter_stability`` (``backtest/walk_forward.py``)
#: mean anything. Below this, both measures are themselves built from too
#: few data points to be trusted: ``parameter_sensitivity`` already refuses
#: to assess anything from fewer than 3 parameter sets, and a
#: window-to-window "stability" or "efficiency ratio" read from only 2-3
#: windows is exactly the kind of small-n statistic this project's own
#: charter is built to distrust (see ``leaderboard.py``'s 100-trade floor
#: for the same instinct at the trade level). 4-6 is the number commonly
#: used as a floor for the analogous k-fold cross-validation case in
#: applied ML practice (rarely fewer than 5 folds trusted); this module
#: uses 6 as a reasoned, not arbitrary, minimum in that same spirit.
MIN_WINDOWS_FOR_STABLE_READ: int = 6


def _total_bars_for_n_windows(n_windows: int) -> int:
    """Bars required for ``n_windows`` walk-forward windows at the real
    ``options_walk_forward`` train/test/step sizing.

    Mirrors ``backtest.walk_forward.generate_windows``'s own arithmetic
    (``while start + train_bars + test_bars <= total_bars: start += step``)
    solved for the total needed to produce exactly ``n_windows``, rather
    than re-deriving a different formula.
    """
    if n_windows <= 0:
        return 0
    return STEP_BARS * (n_windows - 1) + TRAIN_BARS + TEST_BARS


def _required_n(sigma_pct: float, delta_pct: float) -> int:
    """Two-sided one-sample sample-size formula: n = ((z_a/2+z_b)^2 * sigma^2) / delta^2.

    Standard normal-approximation sample-size formula for detecting a mean
    different from zero (a trade sequence's mean net P&L against the null
    of no edge) at the given dispersion and minimum detectable effect.
    """
    if delta_pct <= 0:
        return 0
    z_term = (Z_ALPHA_HALF + Z_BETA) ** 2
    return math.ceil(z_term * (sigma_pct**2) / (delta_pct**2))


@dataclass(frozen=True)
class DatasetRequirement:
    """The computed minimum historical dataset size for options research.

    Every field here is either imported from a real project constant,
    computed from one via shown arithmetic, or an explicitly reasoned
    working assumption -- see ``reasoning_notes`` and
    ``unverified_assumptions`` for which is which.
    """

    min_trading_days: int
    min_distinct_expiries: int
    min_strikes_per_expiry: int
    bar_interval_minutes: int
    instruments: list[str]
    estimated_row_count: int
    estimated_storage_mb: float
    min_trades_for_statistical_power: int
    charter_min_trades: int
    power_analysis_assumptions: dict[str, Any]
    reasoning_notes: list[str]
    unverified_assumptions: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_trading_days": self.min_trading_days,
            "min_distinct_expiries": self.min_distinct_expiries,
            "min_strikes_per_expiry": self.min_strikes_per_expiry,
            "bar_interval_minutes": self.bar_interval_minutes,
            "instruments": list(self.instruments),
            "estimated_row_count": self.estimated_row_count,
            "estimated_storage_mb": round(self.estimated_storage_mb, 2),
            "min_trades_for_statistical_power": self.min_trades_for_statistical_power,
            "charter_min_trades": self.charter_min_trades,
            "power_analysis_assumptions": self.power_analysis_assumptions,
            "reasoning_notes": list(self.reasoning_notes),
            "unverified_assumptions": list(self.unverified_assumptions),
        }


def compute_requirement() -> DatasetRequirement:
    """Build the dataset requirement from real charter/walk-forward constants.

    Returns:
        A :class:`DatasetRequirement` with every number traceable to either
        an import (see the module-level constants above) or a clearly
        labeled reasoned assumption.
    """
    # -- Cost floor: what net return must clear before it is worth calling
    # an "edge" at all, computed live from the real cost model rather than
    # assumed.
    breakeven_move = DEFAULT_COST_MODEL.breakeven_move(
        "OPTION", _ILLUSTRATIVE_PREMIUM, _ILLUSTRATIVE_LOT_QTY, "LONG"
    )
    cost_floor_pct_of_premium = round(breakeven_move / _ILLUSTRATIVE_PREMIUM * 100.0, 3)

    # -- Power analysis: required n for each candidate effect size.
    required_n_by_effect_size = {
        es: _required_n(ASSUMED_SIGMA_PCT_OF_PREMIUM, es) for es in EFFECT_SIZES_PCT_OF_PREMIUM
    }
    recommended_effect_size_pct = round(cost_floor_pct_of_premium * _RECOMMENDED_EFFECT_SIZE_MULTIPLE, 3)
    recommended_n = _required_n(ASSUMED_SIGMA_PCT_OF_PREMIUM, recommended_effect_size_pct)

    # -- Walk-forward bar requirement. The options walk-forward harness
    # (BuyAndHold-per-window) realizes ~1 trade per test window (one
    # position opened and closed within each test slice), so reaching
    # MIN_OOS_TRADES combined out-of-sample trades on that specific path
    # needs roughly MIN_OOS_TRADES windows -- a much larger figure than
    # the MIN_WINDOWS_FOR_STABLE_READ floor that governs when
    # efficiency_ratio/parameter_stability become readable at all.
    windows_for_stability = MIN_WINDOWS_FOR_STABLE_READ
    windows_for_oos_trade_floor = MIN_OOS_TRADES
    bars_for_stability = _total_bars_for_n_windows(windows_for_stability)
    bars_for_oos_trade_floor = _total_bars_for_n_windows(windows_for_oos_trade_floor)
    binding_bars = max(bars_for_stability, bars_for_oos_trade_floor)

    days_1min_for_binding = math.ceil(binding_bars / BARS_PER_SESSION_1MIN)
    days_5min_for_binding = math.ceil(binding_bars / BARS_PER_SESSION_5MIN)

    # -- Recommended calendar coverage. Balances two independent
    # requirements that this codebase itself keeps separate
    # (research/decision.py's own ordering: trade count, then regime
    # count, as two different gates): enough trades pooled across all
    # three instruments to have a chance at approaching statistical power,
    # and enough calendar time to plausibly pass through more than
    # MIN_REGIME_COUNT of the N_REGIME_LABELS-1 substantive regimes
    # (excluding UNKNOWN, which is a refusal-to-classify label, not a
    # market state). One full standard trading year is used as the
    # pragmatic recommendation; reasoning_notes states explicitly that the
    # full statistical-power target likely needs more.
    min_trading_days = TRADING_DAYS_PER_YEAR_APPROX

    # -- Expiry cadence, verified live (2026-09-20): NIFTY weekly (Tuesday),
    # BANKNIFTY monthly-only (last Tuesday of the month, weekly discontinued
    # 1 Sep 2025), SENSEX weekly (Thursday). Source: NSE/BSE-reported
    # coverage of the Sep 2025 SEBI-mandated Tuesday/Thursday expiry
    # restructuring. Flagged as VERIFIED-BUT-VOLATILE, not UNVERIFIED, since
    # this cadence has already changed multiple times by regulatory mandate
    # in recent years and could change again -- treat as current, not
    # permanent.
    min_distinct_expiries = 12  # ~1 year of BANKNIFTY monthlies; ~1 quarter of NIFTY/SENSEX weeklies

    # -- Row count / storage estimate, at both candidate bar intervals.
    def _rows_and_storage(bars_per_day: int) -> tuple[int, float]:
        rows = (
            min_trading_days
            * AVG_LISTED_EXPIRIES_PER_DAY
            * STRIKES_PER_EXPIRY
            * 2  # CE/PE
            * bars_per_day
            * len(INSTRUMENTS)
        )
        storage_mb = rows * ESTIMATED_BYTES_PER_ROW / (1024 * 1024)
        return rows, storage_mb

    rows_1min, storage_mb_1min = _rows_and_storage(BARS_PER_SESSION_1MIN)
    rows_5min, storage_mb_5min = _rows_and_storage(BARS_PER_SESSION_5MIN)

    # 5-minute bars recommended: ~5x cheaper in storage while still fine
    # grained enough for the multi-bar hold strategies this project's
    # walk-forward path targets (test_bars=20 at 5-minute bars already
    # spans less than half a session; 1-minute would only matter for a
    # strategy needing sub-5-minute entry/exit precision).
    recommended_interval_minutes = 5
    estimated_row_count = rows_5min
    estimated_storage_mb = storage_mb_5min

    power_analysis_assumptions: dict[str, Any] = {
        "alpha": 0.05,
        "power": 0.80,
        "z_alpha_half": Z_ALPHA_HALF,
        "z_beta": Z_BETA,
        "assumed_sigma_pct_of_premium": ASSUMED_SIGMA_PCT_OF_PREMIUM,
        "sigma_label": (
            "REASONED WORKING ASSUMPTION, not measured -- see "
            "ASSUMED_SIGMA_PCT_OF_PREMIUM's docstring."
        ),
        "cost_floor_pct_of_premium": cost_floor_pct_of_premium,
        "cost_floor_label": (
            "COMPUTED from backtest.costs.DEFAULT_COST_MODEL.breakeven_move "
            f"on an illustrative {_ILLUSTRATIVE_PREMIUM:g} premium / "
            f"{_ILLUSTRATIVE_LOT_QTY:g}-qty round trip."
        ),
        "effect_sizes_pct_of_premium": EFFECT_SIZES_PCT_OF_PREMIUM,
        "required_n_by_effect_size_pct": {str(k): v for k, v in required_n_by_effect_size.items()},
        "recommended_effect_size_pct": recommended_effect_size_pct,
        "recommended_effect_size_label": (
            f"{_RECOMMENDED_EFFECT_SIZE_MULTIPLE:g}x the computed cost floor -- a reasoned "
            "'modest edge worth having' threshold, not a sourced constant."
        ),
        "recommended_n": recommended_n,
        "iid_caveat": (
            "This is an i.i.d.-asymptotic power calculation. Real trade sequences have "
            "serial correlation / regime clustering (backtest/monte_carlo.py's own "
            "documented caveat: 'IID resampling destroys loss clustering, so these "
            "figures are optimistic'). The true required n is likely higher than "
            "computed here; this is a stated limitation, not corrected for by "
            "arbitrarily inflating the number."
        ),
    }

    reasoning_notes = [
        f"Charter trade floor: leaderboard.MIN_TRADES_FOR_STATUS = {CHARTER_MIN_TRADES} "
        "(also research.decision.decide's default min_trade_count and "
        "TradingMetrics.sufficient_sample's 100-trade floor -- three call sites agree "
        "on the same number).",
        f"Walk-forward window sizing (research.backtest.options_walk_forward."
        f"OptionsWalkForwardRequest defaults): train_bars={TRAIN_BARS}, test_bars={TEST_BARS}, "
        f"step_bars=None -> effective step={STEP_BARS} (backtest.walk_forward.generate_windows "
        "treats a None step as test_bars, i.e. non-overlapping test slices).",
        f"backtest.walk_forward.MIN_OUT_OF_SAMPLE_TRADES = {MIN_OOS_TRADES}: the absolute "
        "out-of-sample trade floor a walk-forward verdict needs to pass "
        "(backtest.walk_forward._verdict), independent of the efficiency ratio.",
        f"MIN_WINDOWS_FOR_STABLE_READ = {MIN_WINDOWS_FOR_STABLE_READ} (reasoned, by analogy to "
        "the ~5-fold floor commonly trusted in k-fold cross-validation): fewer windows than "
        "this and parameter_stability's per-parameter 'distinct'/'stable' counts and "
        "efficiency_ratio's average are themselves computed from too few points to trust "
        "-- backtest.walk_forward.parameter_sensitivity already refuses to assess anything "
        "from fewer than 3 parameter sets for the same reason, one level down.",
        f"That needs {bars_for_stability} bars structurally (generate_windows' own arithmetic, "
        f"solved for {windows_for_stability} windows).",
        "The options_walk_forward harness runs BuyAndHold per test window (one position "
        "opened and closed within the window), so it realizes roughly 1 trade per window "
        "-- not the many-trades-per-window a signal-based intraday strategy might produce. "
        f"Reaching MIN_OUT_OF_SAMPLE_TRADES={MIN_OOS_TRADES} combined out-of-sample trades on "
        f"that path therefore needs on the order of {windows_for_oos_trade_floor} windows, "
        f"i.e. {bars_for_oos_trade_floor} bars -- far more than the "
        f"{bars_for_stability}-bar structural minimum for a stable efficiency-ratio read. "
        "This is a genuine finding, not a restatement: the two gates in "
        "backtest.walk_forward._verdict (efficiency ratio and the absolute OOS-trade floor) "
        "bind at very different bar counts for this specific (BuyAndHold-per-window) harness.",
        f"At 1-minute bars ({BARS_PER_SESSION_1MIN}/session, from "
        "research.data.upstox.calendar.expected_bar_count against the real 375-minute NSE "
        f"session), the binding {binding_bars}-bar requirement is ~{days_1min_for_binding} "
        f"trading days. At 5-minute bars ({BARS_PER_SESSION_5MIN}/session), it is "
        f"~{days_5min_for_binding} trading days.",
        f"A single weekly-expiry option contract only trades for roughly one week "
        "(~5 trading days) of its own life at meaningful liquidity, so the 5-minute-bar "
        f"figure above (~{days_5min_for_binding} days) cannot be satisfied from ONE contract's "
        "own history at all -- reaching the out-of-sample-trade floor requires pooling trades "
        "across many expiries/contracts (the same expiries this module is already sizing a "
        "backfill for), not extending a single contract's recorded window.",
        f"Regime context (research.regime.engine): RegimeDetector can assign "
        f"{N_REGIME_LABELS} labels ({sorted(ALL_REGIMES)}), confirmed only after "
        f"{REGIME_CONFIRMATION_BARS} consecutive matching raw readings (hysteresis). "
        f"research.decision.decide's own min_regime_count default is {MIN_REGIME_COUNT} -- a "
        "conservative floor requiring only 2 of the 7 possible labels. Observing several of "
        "the substantive regimes (TREND_UP, TREND_DOWN, RANGE, HIGH_VOLATILITY, "
        "LOW_VOLATILITY -- excluding UNKNOWN and EVENT_RISK, which are refusal/override "
        "labels rather than market states) plausibly needs several months of calendar time, "
        "since NSE trend/volatility regimes typically persist for multiple weeks at a time "
        "-- this is a REASONED, not computed, claim.",
        f"Trading-days-per-year approximation: {TRADING_DAYS_PER_YEAR_APPROX} (the standard "
        "~52-week x 5-session-minus-holidays rule of thumb used across quantitative finance, "
        "cited as a well-known approximation, not measured from this project's own NSE "
        "holiday calendar).",
        f"Recommended min_trading_days = {min_trading_days} (one full standard trading year): "
        "a pragmatic middle ground between the regime-diversity reasoning above and the "
        "much larger statistical-power-driven trade count computed in the power analysis "
        "below. This is stated explicitly as a floor, not a guarantee of adequacy -- see "
        "'unverified_assumptions' and the power-analysis section for why the full "
        "recommended trade count likely needs more than one year of pooled history at "
        "realistic trade frequencies.",
        "Expiry cadence verified live (web search, 2026-09-20): NIFTY weekly (Tuesday), "
        "BANKNIFTY monthly-only (weekly discontinued 1 Sep 2025), SENSEX weekly (Thursday) "
        "-- the Sep 2025 SEBI-mandated Tuesday/Thursday restructuring. This has changed by "
        "regulatory mandate multiple times in recent years and could change again; treated "
        "as current, not permanent.",
        f"min_distinct_expiries = {min_distinct_expiries}: roughly one year of BANKNIFTY "
        "monthly expiries, or one quarter of NIFTY/SENSEX weeklies -- chosen so the floor is "
        "achievable for the slowest-cadence instrument within the same one-year window "
        "recommended above.",
        f"Strikes per expiry: ATM +/- 10 ({STRIKES_PER_EXPIRY} strikes) -- REASONED, not "
        "sourced. research.strategies.option_selection.py implements ATM / ATM+1 / ATM-1 / "
        "DELTA *selection methods* but no fixed scan width; no other module in this project "
        "states a strike-count default.",
        f"Instruments: {INSTRUMENTS} -- the project's own stated scope "
        "(UPSTOX_INTEGRATION_GUIDE.md, core.upstox_instruments's NSE_INDEX/BSE_INDEX handling).",
        f"Row-count formula: min_trading_days x avg_listed_expiries_per_day "
        f"({AVG_LISTED_EXPIRIES_PER_DAY}, REASONED -- how many expiries a backfill tracks "
        "simultaneously per day, not the total distinct expiries occurring across the whole "
        "year, since any one expiry contract only trades during its own listed life) x "
        f"strikes_per_expiry ({STRIKES_PER_EXPIRY}) x 2 (CE/PE) x bars_per_day x instruments "
        f"({len(INSTRUMENTS)}).",
        f"At 1-minute bars: {min_trading_days} x {AVG_LISTED_EXPIRIES_PER_DAY} x "
        f"{STRIKES_PER_EXPIRY} x 2 x {BARS_PER_SESSION_1MIN} x {len(INSTRUMENTS)} = "
        f"{rows_1min:,} rows (~{storage_mb_1min:,.1f} MB at an estimated "
        f"{ESTIMATED_BYTES_PER_ROW:g} bytes/row including both declared indexes).",
        f"At 5-minute bars: {min_trading_days} x {AVG_LISTED_EXPIRIES_PER_DAY} x "
        f"{STRIKES_PER_EXPIRY} x 2 x {BARS_PER_SESSION_5MIN} x {len(INSTRUMENTS)} = "
        f"{rows_5min:,} rows (~{storage_mb_5min:,.1f} MB).",
        f"Recommended interval: {recommended_interval_minutes}-minute bars (~5x cheaper in "
        "storage than 1-minute, while still fine-grained enough for the multi-bar-hold "
        f"strategies this project's walk-forward path targets -- test_bars={TEST_BARS} at "
        "5-minute bars already spans less than half an NSE session). 1-minute is noted as "
        "the alternative for a strategy that specifically needs sub-5-minute entry/exit "
        "precision.",
        f"Cost floor (COMPUTED): backtest.costs.DEFAULT_COST_MODEL.breakeven_move on an "
        f"illustrative {_ILLUSTRATIVE_PREMIUM:g}-premium / {_ILLUSTRATIVE_LOT_QTY:g}-qty "
        f"round trip = {cost_floor_pct_of_premium}% of premium. A trade's net return must "
        "clear this before any profit is real.",
        f"Return dispersion (REASONED, not measured): sigma = {ASSUMED_SIGMA_PCT_OF_PREMIUM}% "
        "of premium, taken from the illustrative 30-50% short-window premium swings in "
        "tests/research/test_option_economics_vs_direction.py's synthetic scenario. A real "
        "sigma must eventually come from real collected trades (the Upstox backfill this "
        "module's requirement feeds).",
        f"Power calculation: n = ((z_a/2 + z_b)^2 x sigma^2) / delta^2, with "
        f"z_a/2={Z_ALPHA_HALF:.4f} (95% two-sided), z_b={Z_BETA:.4f} (80% power), "
        f"sigma={ASSUMED_SIGMA_PCT_OF_PREMIUM}%. Results by candidate minimum effect size "
        "(delta, as % of premium): "
        + "; ".join(f"delta={k}% -> n={v:,}" for k, v in required_n_by_effect_size.items()) + ".",
        f"Recommended effect size = {recommended_effect_size_pct}% "
        f"({_RECOMMENDED_EFFECT_SIZE_MULTIPLE:g}x the computed cost floor) -> "
        f"required n = {recommended_n:,}.",
        f"COMPARISON: the charter's own MIN_TRADES_FOR_STATUS is {CHARTER_MIN_TRADES}. The "
        f"power calculation above requires {recommended_n:,} trades to reliably (95% "
        f"confidence, 80% power) distinguish a {recommended_effect_size_pct}%-of-premium "
        f"edge from noise at the assumed {ASSUMED_SIGMA_PCT_OF_PREMIUM}% dispersion. "
        f"{CHARTER_MIN_TRADES} trades is only adequate, by this same formula, for effect "
        "sizes at the very large end of the tested range (see "
        "required_n_by_effect_size_pct) -- the charter's 100-trade floor is a reasonable "
        "'stop pretending you know anything below this' floor (and matches this project's "
        "own framing of it as exactly that: 'treat any metric here as indicative only'), "
        "but it is NOT, by this power calculation, adequate to reliably detect a modest, "
        "realistic post-cost edge. These two numbers disagree, and that disagreement is "
        "the intended finding of this analysis, not an error to reconcile.",
        "Caveat (stated, not corrected for): this is an i.i.d.-asymptotic calculation. Real "
        "trade sequences cluster by regime (backtest/monte_carlo.py's own documented "
        "caveat about block-bootstrap vs IID resampling: 'IID resampling destroys loss "
        "clustering, so these figures are optimistic'). The true required n is likely "
        "higher than what is computed here.",
    ]

    unverified_assumptions = [
        "ASSUMED_SIGMA_PCT_OF_PREMIUM (30% per-trade return dispersion): a REASONED WORKING "
        "ASSUMPTION for sizing purposes, not measured from real trade data -- the project's "
        "real database is empty. Must be replaced by a measurement from real collected "
        "trades once available.",
        "AVG_LISTED_EXPIRIES_PER_DAY = 3 (how many expiries a backfill tracks "
        "simultaneously per trading day): reasoned, not derived from any existing project "
        "constant or measurement.",
        f"STRIKES_PER_EXPIRY = {STRIKES_PER_EXPIRY} (ATM +/- 10): reasoned working range; no "
        "strategy generator in this codebase currently hardcodes a strike-count default.",
        f"ESTIMATED_BYTES_PER_ROW = {ESTIMATED_BYTES_PER_ROW:g}: estimated from the `bars` "
        "table's column list in research/data/upstox/store.py, not measured against a "
        "populated database (the real store is empty).",
        "The options_walk_forward harness realizing ~1 trade per test window (used to "
        "derive the OOS-trade-floor bar requirement): a reasoned inference from reading "
        "BuyAndHold's behavior in research/backtest/options_walk_forward.py, not an "
        "independently measured trade-frequency figure.",
        "The i.i.d.-independence assumption underlying the entire power calculation in "
        "power_analysis_assumptions: documented as optimistic per backtest/monte_carlo.py's "
        "own caveat about serial correlation / loss clustering, not corrected for.",
        "Expiry cadence (NIFTY weekly/BANKNIFTY monthly/SENSEX weekly): verified via a live "
        "web search on 2026-09-20 reflecting the Sep 2025 SEBI-mandated restructuring, but "
        "this cadence has changed by regulatory mandate multiple times in recent years and "
        "is not guaranteed to still hold at the time this module is read -- re-verify before "
        "relying on it for a live backfill plan.",
    ]

    return DatasetRequirement(
        min_trading_days=min_trading_days,
        min_distinct_expiries=min_distinct_expiries,
        min_strikes_per_expiry=STRIKES_PER_EXPIRY,
        bar_interval_minutes=recommended_interval_minutes,
        instruments=list(INSTRUMENTS),
        estimated_row_count=estimated_row_count,
        estimated_storage_mb=estimated_storage_mb,
        min_trades_for_statistical_power=recommended_n,
        charter_min_trades=CHARTER_MIN_TRADES,
        power_analysis_assumptions=power_analysis_assumptions,
        reasoning_notes=reasoning_notes,
        unverified_assumptions=unverified_assumptions,
    )
