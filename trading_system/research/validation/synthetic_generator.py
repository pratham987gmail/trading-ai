"""Synthetic option-market data generator with known, planted statistical properties.

Everything downstream in this package (``scenarios.py``, ``runner.py``) exists
to answer one question: does the *real* research pipeline reach the *correct*
conclusion about data whose true properties we already know, because we built
them? This module is where those true properties get built.

**Design: one "session" = one independent option contract.**

``backtest.strategies.BuyAndHold`` (the only strategy
``research.backtest.options_backtest.run_options_backtest`` drives) enters
once and holds to the end of the series it is given -- so a single call to
``run_options_backtest`` against a single instrument produces exactly one
completed trade. The charter's own significance floor
(``research.decision.decide``'s ``min_trade_count=100``) cannot be cleared by
one trade. This generator therefore does not model one giant continuous
option series; it models ``n_sessions`` independent option contracts (each
with its own strike, encoded as ``20000 + session_index`` so every session is
individually addressable through the ``HistoricalDataProvider`` ABC), each
``bars_per_session`` bars long. A caller (``research.validation.runner``)
runs a real ``run_options_backtest`` once per session and pools the resulting
per-trade net P&L into ``research.ml.evaluation.trading_metrics`` -- the same
"many independent trials pooled into one metrics object" shape a real
multi-day recorded dataset would eventually produce, just compressed into one
synthetic call. This is a harness design choice, not a pipeline change: every
individual trade is still produced by the real, unmodified
``BacktestEngine``/``CostModel``.

For walk-forward (which needs many bars of *one* continuous instrument, not
many one-trade instruments), a separate long single-strike series is exposed
under the reserved strike ``99999`` (see :meth:`SyntheticHistoricalProvider`
docstring) spanning the whole configured history.

**Pricing.** Every option premium is a real Black-Scholes-Merton price
(:func:`analysis.options_analytics.black_scholes_price`, reused rather than
reimplemented) evaluated against a generated underlying path, a generated IV
path, and calendar time-to-expiry -- never intrinsic value. This is what
allows "correct direction, losing trade" (theta/IV decay eating a correct
directional call) to actually occur in generated data, exactly as
``tests/research/test_option_economics_vs_direction.py`` demonstrates by
hand for one worked example.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from analysis.options_analytics import black_scholes_price
from research.data.provider import HistoricalDataProvider, OptionType

#: Minutes in one trading session (375 = a standard NSE 09:15-15:30 session).
SESSION_MINUTES = 375.0

#: Reserved strike identifying the long continuous single-instrument series
#: used for walk-forward testing (see module docstring). Chosen far outside
#: the ``20000 + session_index`` per-session strike range so it can never
#: collide with a real session strike for any configured ``n_sessions``.
WALK_FORWARD_STRIKE = 99999.0

#: Annualised, continuously-compounded risk-free rate used for every
#: Black-Scholes evaluation. Not a config field: it is not something any
#: scenario needs to vary, and fixing it keeps every scenario's option
#: pricing directly comparable.
RISK_FREE_RATE = 0.06

TradeSide = Literal["CE", "PE"]


@dataclass(frozen=True)
class SyntheticScenarioConfig:
    """Every knob controlling one synthetic dataset, fully reproducible from ``seed``.

    See the module docstring for the session/pricing model this config feeds.
    Attributes are documented individually only where their meaning is not
    obvious from the phase brief that specified this dataclass verbatim.
    """

    seed: int
    n_sessions: int
    bars_per_session: int
    underlying_start_price: float
    drift_annualized: float
    volatility_annualized: float
    vol_clustering: float
    regime_switch_prob: float
    iv_base: float
    iv_term_structure_slope: float
    iv_vol_response: float
    theta_decay_model: str = "black_scholes_like"
    spread_base_bps: float = 20.0
    spread_atm_widening_factor: float = 0.5
    spread_open_close_widening_factor: float = 1.5
    bad_tick_rate: float = 0.0
    missing_bar_rate: float = 0.0
    gap_rate: float = 0.0
    planted_feature: str | None = None
    planted_effect_size: float | None = None
    planted_effect_regime: str | None = None
    planted_effect_window: tuple[int, int] | None = None
    lookahead_leak: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # tuples survive asdict() as tuples; JSON needs lists for fingerprint
        # stability across a tuple/list round trip.
        if data["planted_effect_window"] is not None:
            data["planted_effect_window"] = list(data["planted_effect_window"])
        return data

    def fingerprint(self) -> str:
        """Deterministic hash of every field, for the report to key results by."""
        payload = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _garch_like_variance_path(
    rng: np.random.Generator, n: int, base_var: float, persistence: float
) -> np.ndarray:
    """A simple GARCH(1,1)-flavoured variance recursion for volatility clustering.

    Not a maximum-likelihood-fit GARCH model -- this is a generator, not an
    estimator. The recursion is: ``var[t] = (1-persistence)*base_var +
    persistence*var[t-1]*(1 + 0.5*(shock[t-1]**2 - 1))``, which reverts to
    ``base_var`` when ``persistence=0`` and exhibits volatility clustering
    (a large shock raises subsequent variance) as ``persistence -> 1``.
    Documented here, not asserted to be a "real" GARCH fit.
    """
    variances = np.empty(n)
    variances[0] = base_var
    prev_shock = 0.0
    for t in range(1, n):
        variances[t] = max(
            1e-12,
            (1 - persistence) * base_var
            + persistence * variances[t - 1] * (1 + 0.5 * (prev_shock**2 - 1)),
        )
        prev_shock = rng.standard_normal()
    return variances


def _bar_ohlc_from_close(
    rng: np.random.Generator, prev_close: float, close: float, intrabar_vol: float
) -> tuple[float, float, float]:
    """Synthesize an ``(open, high, low)`` around a bar's generated close.

    ``open`` is the previous bar's close (continuous, no artificial jump
    unless a gap is injected separately); ``high``/``low`` bracket
    ``min/max(open, close)`` by a small random excursion so ``Bar.is_valid``
    always holds.
    """
    open_ = prev_close
    excursion = abs(close - open_) * 0.3 + intrabar_vol * open_ * abs(rng.standard_normal()) * 0.1
    high = max(open_, close) + abs(excursion)
    low = min(open_, close) - abs(excursion)
    low = max(low, 0.01)
    return open_, high, low


class SyntheticHistoricalProvider(HistoricalDataProvider):
    """A fully synthetic, deterministic implementation of the real provider ABC.

    Generates option premium bars from a real Black-Scholes pricer driven by
    a generated underlying path and IV path -- never intrinsic value -- so
    the research layer sees the same DataFrame shapes
    (``open``/``high``/``low``/``close``/``volume`` OHLCV, ``DatetimeIndex``,
    ascending) that :class:`research.data.provider.OptionStoreProvider` and
    :class:`research.data.upstox.provider.UpstoxHistoricalProvider` produce,
    and must not be able to tell the difference.

    Every session is independently addressable as its own "contract" via the
    strike ``20000 + session_index`` (see module docstring for why). A
    reserved long continuous series is available at strike
    :data:`WALK_FORWARD_STRIKE` for walk-forward testing.

    All randomness is drawn from ``numpy.random.default_rng(config.seed)`` in
    a single, deterministic pass computed lazily on first access and cached,
    so the same config always yields byte-identical data and repeated calls
    are cheap.
    """

    def __init__(self, config: SyntheticScenarioConfig) -> None:
        self.config = config
        self._underlying_name = "SYNTH"
        self._expiry = "31DEC2026"
        self._option_type: TradeSide = "CE"
        self._built = False

    # ------------------------------------------------------------------
    # Lazy, deterministic construction
    # ------------------------------------------------------------------

    def _ensure_built(self) -> None:
        if self._built:
            return
        cfg = self.config
        rng = np.random.default_rng(cfg.seed)

        bars_per_year = 252 * cfg.bars_per_session
        per_bar_vol = cfg.volatility_annualized / math.sqrt(max(bars_per_year, 1))
        per_bar_drift = cfg.drift_annualized / max(bars_per_year, 1)
        base_var = per_bar_vol**2

        total_bars = cfg.n_sessions * cfg.bars_per_session
        variances = _garch_like_variance_path(rng, total_bars, base_var, cfg.vol_clustering)

        # Regime: a persistent drift-sign flag, flipping with probability
        # regime_switch_prob per bar. This is deliberately simpler than
        # research.regime.engine.RegimeDetector (which classifies from price
        # structure) -- it is the *generative* mechanism the detector is
        # later asked to recover, not a copy of the detector itself.
        drift_sign = 1.0
        drift_signs = np.empty(total_bars)
        for t in range(total_bars):
            if rng.random() < cfg.regime_switch_prob:
                drift_sign *= -1.0
            drift_signs[t] = drift_sign

        # Per-session planted signal: a clean +1/-1 bucket assignment (not a
        # continuous proxy) so the ground-truth high/low bucket split used by
        # the scenario builders and the sanity checks is unambiguous.
        session_signals = rng.choice([-1.0, 1.0], size=cfg.n_sessions)

        shocks = rng.standard_normal(total_bars)
        returns = per_bar_drift + drift_signs * 0.0 + np.sqrt(variances) * shocks
        # (drift_signs currently only used for the regime label below; the
        # directional regime itself is expressed through per-bar volatility
        # dominance rather than doubling the drift, so a "trend" regime here
        # is a real, measurable price-structure trend a RegimeDetector can
        # pick up, not merely a hidden variable.)
        trending_component = per_bar_drift * 4.0 * drift_signs
        returns = returns + trending_component

        # Plant a per-session directional edge tied to session_signals, when
        # requested. Applied as an additive per-bar drift confined to the
        # session's own bars (and, for scenario 3/5, confined further to a
        # bar window / regime) -- see scenarios.py for how each scenario
        # configures this.
        session_generative_regime: list[str] = []
        if cfg.planted_feature is not None and cfg.planted_effect_size:
            for session_idx in range(cfg.n_sessions):
                start = session_idx * cfg.bars_per_session
                end = start + cfg.bars_per_session
                avg_drift_sign = float(np.mean(drift_signs[start:end]))
                generative_regime = "TREND_UP" if avg_drift_sign > 0 else "TREND_DOWN"
                session_generative_regime.append(generative_regime)

                if cfg.planted_effect_regime is not None and generative_regime != cfg.planted_effect_regime:
                    # Scenario 5 (regime-conditional edge): the plant is
                    # confined to sessions whose generative regime matches
                    # planted_effect_regime; every other session gets none of
                    # the boost, so an unconditional pool washes it out.
                    continue

                lo, hi = 0, cfg.bars_per_session
                if cfg.planted_effect_window is not None:
                    lo, hi = cfg.planted_effect_window
                    lo = max(0, min(lo, cfg.bars_per_session))
                    hi = max(lo, min(hi, cfg.bars_per_session))
                if hi <= lo:
                    continue
                window_len = hi - lo
                boost = (
                    session_signals[session_idx]
                    * (cfg.planted_effect_size / 100.0)
                    / window_len
                )
                returns[start + lo : start + hi] += boost
        else:
            for session_idx in range(cfg.n_sessions):
                start = session_idx * cfg.bars_per_session
                end = start + cfg.bars_per_session
                avg_drift_sign = float(np.mean(drift_signs[start:end]))
                session_generative_regime.append("TREND_UP" if avg_drift_sign > 0 else "TREND_DOWN")

        log_spot = np.log(cfg.underlying_start_price) + np.cumsum(returns)
        spot_path = np.exp(log_spot)

        # Session start timestamps: one synthetic trading day apart, bars
        # every (SESSION_MINUTES / bars_per_session) minutes within a day.
        base_date = datetime(2025, 1, 6, 9, 15)  # a Monday
        bar_minutes = SESSION_MINUTES / cfg.bars_per_session

        timestamps: list[datetime] = []
        for session_idx in range(cfg.n_sessions):
            day = base_date + timedelta(days=session_idx)
            for b in range(cfg.bars_per_session):
                timestamps.append(day + timedelta(minutes=b * bar_minutes))

        # Opening gaps: applied per-session to the first bar's underlying
        # level only (a real overnight-gap-like discontinuity), independent
        # of the planted drift.
        for session_idx in range(cfg.n_sessions):
            if rng.random() < cfg.gap_rate:
                start = session_idx * cfg.bars_per_session
                gap_pct = rng.normal(0, 0.01)
                spot_path[start:] *= (1 + gap_pct)

        self._spot_path = spot_path
        self._timestamps = timestamps
        self._session_signals = session_signals

        # IV path: base + term-structure slope on days-to-expiry + a
        # leverage/vol-of-vol response to the underlying's own return.
        session_frames: dict[int, pd.DataFrame] = {}
        session_meta: list[dict[str, Any]] = []
        underlying_frames: dict[int, pd.DataFrame] = {}

        # A fixed, short (2-trading-day) tenor per session -- each session is
        # its own short-dated contract, not a single option observed across
        # the whole dataset (that role is reserved for WALK_FORWARD_STRIKE).
        dte_start_days = 2.0

        regime_underlying_rows: list[dict[str, Any]] = []

        for session_idx in range(cfg.n_sessions):
            start = session_idx * cfg.bars_per_session
            strike = round(spot_path[start] / 50.0) * 50.0
            option_rows = []
            underlying_rows = []
            prev_close = spot_path[start - 1] if start > 0 else spot_path[start]
            for b in range(cfg.bars_per_session):
                idx = start + b
                close_spot = spot_path[idx]
                ret = returns[idx]
                dte_days = max(
                    0.02, dte_start_days - (b / cfg.bars_per_session) * (SESSION_MINUTES / 1440.0)
                )
                iv = max(
                    0.03,
                    cfg.iv_base
                    + cfg.iv_term_structure_slope * dte_days
                    + cfg.iv_vol_response * (-ret)
                    + 0.02 * (math.sqrt(variances[idx]) / max(per_bar_vol, 1e-9) - 1),
                )
                years = dte_days / 365.0
                premium_close = black_scholes_price(
                    close_spot, strike, years, RISK_FREE_RATE, iv, self._option_type
                )
                if premium_close is None or premium_close < 0.05:
                    premium_close = 0.05

                u_open, u_high, u_low = _bar_ohlc_from_close(
                    rng, prev_close, close_spot, math.sqrt(variances[idx])
                )
                # Price the option at the underlying's high and low too, so
                # the option bar's own high/low are genuinely derived from
                # the same BS surface rather than copied ratios of the
                # underlying's range (which would be wrong: options are
                # convex, not linear, in the underlying).
                p_at_high = black_scholes_price(
                    u_high, strike, years, RISK_FREE_RATE, iv, self._option_type
                ) or premium_close
                p_at_low = black_scholes_price(
                    u_low, strike, years, RISK_FREE_RATE, iv, self._option_type
                ) or premium_close
                p_at_open = black_scholes_price(
                    u_open, strike, years, RISK_FREE_RATE, iv, self._option_type
                ) or premium_close

                option_high = max(p_at_high, p_at_low, p_at_open, premium_close)
                option_low = max(0.05, min(p_at_high, p_at_low, p_at_open, premium_close))

                spread = (
                    cfg.spread_base_bps / 10_000.0
                    * premium_close
                    * (1 + cfg.spread_atm_widening_factor * 0.0)
                )
                # Widen near session open/close per spread_open_close_widening_factor,
                # using a simple symmetric linear taper over the first/last 10% of
                # the session -- documented shape, not claimed to match any real
                # microstructure study.
                edge_frac = min(b, cfg.bars_per_session - 1 - b) / max(1, cfg.bars_per_session * 0.1)
                edge_frac = max(0.0, min(1.0, edge_frac))
                open_close_widen = 1 + cfg.spread_open_close_widening_factor * (1 - edge_frac)
                spread *= open_close_widen

                bid = max(0.05, premium_close - spread / 2)
                ask = premium_close + spread / 2

                option_rows.append(
                    {
                        "timestamp": self._timestamps[idx],
                        "open": round(p_at_open, 2),
                        "high": round(option_high, 2),
                        "low": round(option_low, 2),
                        "close": round(premium_close, 2),
                        "volume": int(max(1, rng.poisson(500))),
                        "iv": round(iv, 4),
                        "bid": round(bid, 2),
                        "ask": round(ask, 2),
                    }
                )
                underlying_rows.append(
                    {
                        "timestamp": self._timestamps[idx],
                        "open": round(u_open, 2),
                        "high": round(u_high, 2),
                        "low": round(u_low, 2),
                        "close": round(close_spot, 2),
                        "volume": float("nan"),
                    }
                )
                prev_close = close_spot

            option_frame = pd.DataFrame(option_rows).set_index("timestamp")
            underlying_frame = pd.DataFrame(underlying_rows).set_index("timestamp")

            option_frame = _inject_anomalies(rng, option_frame, cfg)

            session_frames[session_idx] = option_frame
            underlying_frames[session_idx] = underlying_frame
            session_meta.append(
                {
                    "session_index": session_idx,
                    "strike": strike,
                    "planted_signal": float(session_signals[session_idx]),
                    "spot_at_open": float(spot_path[start]),
                    "trading_date": (base_date + timedelta(days=session_idx)).date().isoformat(),
                    "generative_regime": session_generative_regime[session_idx],
                }
            )

        self._session_frames = session_frames
        self._underlying_session_frames = underlying_frames
        self._session_meta = session_meta

        # Reserved long continuous series for walk-forward: a rolling
        # fixed-tenor ATM contract (like a sequence of monthly options,
        # re-struck at the start of each tenor cycle) tracked continuously
        # across the whole configured history. A single option cannot itself
        # span the whole dataset without either running past its own expiry
        # (undefined BS price) or -- if given a genuinely multi-year tenor --
        # making iv_term_structure_slope's per-day contribution to IV blow up
        # far outside the range it was calibrated for in the per-session
        # contracts above. Rolling the tenor keeps days-to-expiry, and hence
        # the term-structure IV term, in the same order of magnitude as every
        # per-session contract throughout.
        # Which sessions feed the walk-forward series: the SAME population
        # research.validation.runner's pooled backtest trades (the
        # high-bucket sessions, when a feature is planted; every session
        # otherwise -- see runner._select_trade_sessions, mirrored here so
        # walk-forward validates the same candidate the charter-metrics gate
        # was computed from, rather than a strategy that unconditionally
        # trades every session regardless of the plant). Low-bucket/unselected
        # sessions' bars are skipped entirely rather than left in with no
        # edge, which would test a different, unfiltered strategy.
        if cfg.planted_feature is not None:
            wf_session_indices = [i for i in range(cfg.n_sessions) if session_signals[i] > 0]
        else:
            wf_session_indices = list(range(cfg.n_sessions))
        wf_source_idx: list[int] = []
        for session_idx in wf_session_indices:
            start = session_idx * cfg.bars_per_session
            wf_source_idx.extend(range(start, start + cfg.bars_per_session))

        tenor_days = 20.0
        bar_day_fraction = SESSION_MINUTES / cfg.bars_per_session / 1440.0
        cycle_bars = max(1, int(round(tenor_days / bar_day_fraction)))
        wf_rows = []
        prev_close = spot_path[wf_source_idx[0]] if wf_source_idx else spot_path[0]
        wf_strike = round(prev_close / 50.0) * 50.0
        wf_base_date = datetime(2030, 1, 1, 9, 15)
        bar_minutes = SESSION_MINUTES / cfg.bars_per_session
        for wf_pos, idx in enumerate(wf_source_idx):
            pos_in_cycle = wf_pos % cycle_bars
            if pos_in_cycle == 0:
                wf_strike = round(spot_path[idx] / 50.0) * 50.0
            dte_days = max(0.05, tenor_days - pos_in_cycle * bar_day_fraction)
            years = dte_days / 365.0
            close_spot = spot_path[idx]
            iv = max(
                0.03,
                cfg.iv_base
                + cfg.iv_term_structure_slope * dte_days
                + cfg.iv_vol_response * (-returns[idx]),
            )
            premium_close = black_scholes_price(
                close_spot, wf_strike, years, RISK_FREE_RATE, iv, self._option_type
            )
            if premium_close is None or premium_close < 0.05:
                premium_close = 0.05
            u_open, u_high, u_low = _bar_ohlc_from_close(
                rng, prev_close, close_spot, math.sqrt(variances[idx])
            )
            p_high = black_scholes_price(u_high, wf_strike, years, RISK_FREE_RATE, iv, self._option_type) or premium_close
            p_low = black_scholes_price(u_low, wf_strike, years, RISK_FREE_RATE, iv, self._option_type) or premium_close
            p_open = black_scholes_price(u_open, wf_strike, years, RISK_FREE_RATE, iv, self._option_type) or premium_close
            wf_rows.append(
                {
                    "timestamp": wf_base_date + timedelta(minutes=wf_pos * bar_minutes),
                    "open": round(_price_or(p_open, premium_close), 2),
                    "high": round(max(p_high, p_low, p_open, premium_close), 2),
                    "low": round(max(0.05, min(p_high, p_low, p_open, premium_close)), 2),
                    "close": round(premium_close, 2),
                    "volume": int(max(1, rng.poisson(500))),
                }
            )
            prev_close = close_spot
        self._wf_frame = pd.DataFrame(wf_rows).set_index("timestamp")
        self._wf_strike = wf_strike

        self._built = True

    # ------------------------------------------------------------------
    # HistoricalDataProvider ABC
    # ------------------------------------------------------------------

    def _session_for_strike(self, strike: float) -> int | None:
        idx = int(round(strike)) - 20000
        if 0 <= idx < self.config.n_sessions:
            return idx
        return None

    def option_bars(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: OptionType,
        *,
        interval_minutes: int = 5,
        price_column: str = "ltp",
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        self._ensure_built()
        if int(round(strike)) == int(WALK_FORWARD_STRIKE):
            return self._wf_frame.copy()
        session_idx = self._session_for_strike(strike)
        if session_idx is None:
            return pd.DataFrame()
        return self._session_frames[session_idx].copy()

    def spread_bars(
        self,
        underlying: str,
        expiry: str,
        long_strike: float,
        short_strike: float,
        option_type: OptionType,
        *,
        interval_minutes: int = 5,
        price_column: str = "ltp",
    ) -> pd.DataFrame:
        long_frame = self.option_bars(underlying, expiry, long_strike, option_type)
        short_frame = self.option_bars(underlying, expiry, short_strike, option_type)
        if long_frame.empty or short_frame.empty:
            return pd.DataFrame()
        common_index = long_frame.index.intersection(short_frame.index)
        if len(common_index) == 0:
            return pd.DataFrame()
        long_frame = long_frame.loc[common_index]
        short_frame = short_frame.loc[common_index]
        net = pd.DataFrame(index=common_index)
        for col in ("open", "high", "low", "close"):
            net[col] = long_frame[col] - short_frame[col]
        net["volume"] = long_frame.get("volume", 0)
        return net

    def list_expiries(self, underlying: str) -> list[str]:
        return [self._expiry]

    def list_underlyings(self) -> list[str]:
        return [self._underlying_name]

    def coverage(self) -> pd.DataFrame:
        self._ensure_built()
        rows = [
            {
                "trade_date": meta["trading_date"],
                "underlying": self._underlying_name,
                "snapshots": self.config.bars_per_session,
            }
            for meta in self._session_meta
        ]
        return pd.DataFrame(rows)

    def get_underlying_bars(
        self,
        underlying: str,
        *,
        interval_minutes: int = 5,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        self._ensure_built()
        frames = [self._underlying_session_frames[i] for i in range(self.config.n_sessions)]
        return pd.concat(frames)

    def get_option_chain(
        self,
        underlying: str,
        expiry: str,
        *,
        at: datetime | str | None = None,
    ) -> dict:
        self._ensure_built()
        session_idx = 0
        frame = self._session_frames.get(session_idx)
        if frame is None or frame.empty:
            return {}
        row = frame.iloc[-1]
        strike = self._session_meta[session_idx]["strike"]
        return {
            "underlying": underlying,
            "expiry": expiry,
            "strikes": {
                strike: {
                    self._option_type: {
                        "ltp": float(row["close"]),
                        "bid": float(row.get("bid", row["close"])),
                        "ask": float(row.get("ask", row["close"])),
                    }
                }
            },
        }

    def get_expiries(self, underlying: str) -> list[str]:
        return self.list_expiries(underlying)

    def get_strikes(self, underlying: str, expiry: str) -> list[float]:
        self._ensure_built()
        return sorted(meta["strike"] for meta in self._session_meta)

    def get_available_contracts(self, underlying: str | None = None) -> pd.DataFrame:
        self._ensure_built()
        rows = [
            {
                "underlying": self._underlying_name,
                "expiry": self._expiry,
                "strike": 20000.0 + meta["session_index"],
                "option_type": self._option_type,
                "first_seen": self._session_frames[meta["session_index"]].index[0],
                "last_seen": self._session_frames[meta["session_index"]].index[-1],
                "bar_count": len(self._session_frames[meta["session_index"]]),
            }
            for meta in self._session_meta
        ]
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # Harness-specific accessors (not part of the ABC): ground-truth data
    # ------------------------------------------------------------------

    def session_metadata(self) -> list[dict[str, Any]]:
        """Per-session ground truth (planted signal, strike, spot) for scenario checks."""
        self._ensure_built()
        return list(self._session_meta)

    def session_strike(self, session_index: int) -> float:
        return 20000.0 + session_index

    def underlying_session_frame(self, session_index: int) -> pd.DataFrame:
        self._ensure_built()
        return self._underlying_session_frames[session_index].copy()

    def walk_forward_bar_count(self) -> int:
        """Length of the reserved walk-forward series (see :data:`WALK_FORWARD_STRIKE`)."""
        self._ensure_built()
        return len(self._wf_frame)


def _price_or(value: float | None, fallback: float) -> float:
    return value if value is not None and value > 0 else fallback


def _inject_anomalies(
    rng: np.random.Generator, frame: pd.DataFrame, cfg: SyntheticScenarioConfig
) -> pd.DataFrame:
    """Post-process a generated OHLCV frame with realistic recording anomalies.

    Three anomaly types, matching the vocabulary
    ``research.data.upstox.validation.validate_bars`` checks for on a real
    provider (not imported here -- this is a different provider -- but the
    anomaly *shapes* are deliberately the same so validation run against
    synthetic data exercises the same code paths a real recorder's output
    would):

    - Single-bar price spike (a "bad tick"): one OHLC value multiplied by an
      outlier factor.
    - Zero/negative price: an OHLC value collapsed to zero.
    - Missing bar: the row dropped entirely.

    Timestamp out-of-order anomalies are not injected here because
    ``BacktestEngine._validate`` hard-rejects a non-monotonic index (by
    design -- see ``backtest/engine.py``), so a caller wanting to exercise
    that failure mode would validate the frame *before* engine ingestion,
    not after.
    """
    if frame.empty:
        return frame
    frame = frame.copy()
    n = len(frame)

    bad_tick_mask = rng.random(n) < cfg.bad_tick_rate
    for i in np.where(bad_tick_mask)[0]:
        col = rng.choice(["open", "high", "low", "close"])
        if rng.random() < 0.5:
            frame.iloc[i, frame.columns.get_loc(col)] *= rng.uniform(5, 20)
        else:
            frame.iloc[i, frame.columns.get_loc(col)] = 0.0

    missing_mask = rng.random(n) < cfg.missing_bar_rate
    keep = ~missing_mask
    # Never drop every bar; a scenario builder controls total data volume
    # deliberately (e.g. the thin-data scenario), this is not that lever.
    if keep.sum() >= max(2, int(n * 0.5)):
        frame = frame.loc[keep]
    return frame
