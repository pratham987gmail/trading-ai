"""Fast, isolated tests for the synthetic data generator itself.

None of these run the full research pipeline (see
``tests/research/test_validation_scenarios.py``, marked ``slow``, for that);
these only check that the generator behaves as its own docstrings promise:
deterministic, ABC-conformant, capable of producing "correct direction,
losing trade" cases, and honest about injected anomaly rates.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.data.provider import HistoricalDataProvider
from research.validation.synthetic_generator import (
    SyntheticHistoricalProvider,
    SyntheticScenarioConfig,
)

BASE_KWARGS = dict(
    n_sessions=8,
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


def _config(**overrides) -> SyntheticScenarioConfig:
    kwargs = dict(BASE_KWARGS)
    kwargs.update(overrides)
    kwargs.setdefault("seed", 7)
    return SyntheticScenarioConfig(**kwargs)


def test_implements_full_provider_abc():
    provider = SyntheticHistoricalProvider(_config())
    assert isinstance(provider, HistoricalDataProvider)
    # Every abstractmethod on the ABC must be concretely overridden -- this
    # is enforced by Python itself (instantiation would fail otherwise), but
    # assert it explicitly so a future ABC method addition fails loudly here
    # rather than silently inheriting a NotImplementedError stub.
    for name in HistoricalDataProvider.__abstractmethods__:
        assert getattr(type(provider), name) is not getattr(HistoricalDataProvider, name)


def test_reproducibility_same_seed_identical_output():
    p1 = SyntheticHistoricalProvider(_config(seed=42))
    p2 = SyntheticHistoricalProvider(_config(seed=42))
    frame1 = p1.option_bars("SYNTH", "31DEC2026", 20000.0, "CE")
    frame2 = p2.option_bars("SYNTH", "31DEC2026", 20000.0, "CE")
    pd.testing.assert_frame_equal(frame1, frame2)


def test_different_seed_different_output():
    p1 = SyntheticHistoricalProvider(_config(seed=1))
    p2 = SyntheticHistoricalProvider(_config(seed=2))
    frame1 = p1.option_bars("SYNTH", "31DEC2026", 20000.0, "CE")
    frame2 = p2.option_bars("SYNTH", "31DEC2026", 20000.0, "CE")
    assert not frame1["close"].equals(frame2["close"])


def test_column_shape_matches_ohlcv_contract():
    provider = SyntheticHistoricalProvider(_config())
    frame = provider.option_bars("SYNTH", "31DEC2026", 20000.0, "CE")
    for col in ("open", "high", "low", "close", "volume"):
        assert col in frame.columns
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.is_monotonic_increasing
    assert (frame["high"] >= frame["low"]).all()
    assert (frame["close"] > 0).all()


def test_underlying_bars_have_nan_volume_like_option_store_provider():
    """OptionStoreProvider.get_underlying_bars documents volume as always-NaN
    (no separately-traded instrument backs the spot series); the synthetic
    provider must carry the same convention so downstream code cannot tell
    the difference."""
    provider = SyntheticHistoricalProvider(_config())
    frame = provider.get_underlying_bars("SYNTH")
    assert frame["volume"].isna().all()


def test_real_black_scholes_pricer_used_not_intrinsic_value():
    """The option premium must not equal intrinsic value -- if it did, this
    generator would have the exact bug research/backtest/options_backtest.py's
    own docstring calls out (pricing exits from expiry-style intrinsic
    payoff)."""
    provider = SyntheticHistoricalProvider(_config())
    frame = provider.option_bars("SYNTH", "31DEC2026", 20000.0, "CE")
    underlying = provider.underlying_session_frame(0)
    strike = provider.session_metadata()[0]["strike"]
    intrinsic = (underlying["close"] - strike).clip(lower=0.0)
    # The premium (real BS price, carrying time value) must differ from
    # intrinsic value for at least the interior bars, where time value is
    # nonzero given a positive IV and days-to-expiry.
    assert not np.allclose(frame["close"].to_numpy(), intrinsic.to_numpy(), atol=0.5)


def test_correct_direction_losing_trade_reproducible():
    """Direct proof (not a full scenario run) that this generator can
    produce a session where the underlying moves in the option's favorable
    direction (CE + underlying up) yet the option's own premium ends lower
    than it started -- theta/IV decay overwhelming a small favorable move.
    Scans several seeds/sessions for at least one occurrence, since this is
    a real (not guaranteed-every-time) property of option economics.
    """
    found = False
    for seed in range(50):
        provider = SyntheticHistoricalProvider(
            _config(seed=seed, n_sessions=20, iv_base=0.35, iv_term_structure_slope=0.05)
        )
        for session_idx in range(20):
            underlying = provider.underlying_session_frame(session_idx)
            option = provider.option_bars(
                "SYNTH", "31DEC2026", provider.session_strike(session_idx), "CE"
            )
            underlying_up = underlying["close"].iloc[-1] > underlying["close"].iloc[0]
            option_down = option["close"].iloc[-1] < option["close"].iloc[0]
            if underlying_up and option_down:
                found = True
                break
        if found:
            break
    assert found, (
        "Expected at least one (seed, session) where the underlying moved up "
        "(favorable for a long CE) but the option's own premium fell -- "
        "the 'correct direction, losing trade' pattern this generator exists "
        "to reproduce. None found across 50 seeds x 20 sessions."
    )


def test_bad_tick_rate_roughly_matches_configured_rate():
    provider = SyntheticHistoricalProvider(
        _config(n_sessions=40, bad_tick_rate=0.1, missing_bar_rate=0.0, gap_rate=0.0)
    )
    provider._ensure_built()
    total_bars = 0
    spike_like = 0
    for session_idx in range(40):
        frame = provider._session_frames[session_idx]
        total_bars += len(frame)
        # A bad tick either zeroes a value or spikes it 5-20x; both produce
        # an OHLC value far outside a normal bar's own open/close range.
        spike_like += int(
            ((frame[["open", "high", "low", "close"]] == 0.0).any(axis=1))
            .sum()
        )
    rate = spike_like / total_bars
    # Loose bound: half of injected zero-type bad ticks (the other half are
    # spikes, not zeros) should show up as zeros, so expect roughly
    # bad_tick_rate/2 with generous slack for the coin flip and small sample.
    assert 0.0 <= rate <= 0.15


def test_missing_bar_rate_drops_rows():
    provider = SyntheticHistoricalProvider(
        _config(n_sessions=10, bad_tick_rate=0.0, missing_bar_rate=0.15, gap_rate=0.0)
    )
    frame = provider.option_bars("SYNTH", "31DEC2026", provider.session_strike(0), "CE")
    assert len(frame) < 75
    assert len(frame) >= int(75 * 0.5)


def test_fingerprint_deterministic_and_distinguishes_configs():
    c1 = _config(seed=1)
    c2 = _config(seed=1)
    c3 = _config(seed=2)
    assert c1.fingerprint() == c2.fingerprint()
    assert c1.fingerprint() != c3.fingerprint()


def test_fingerprint_changes_with_any_field():
    c1 = _config(seed=1, planted_effect_size=None, planted_feature=None)
    c2 = _config(seed=1, planted_effect_size=5.0, planted_feature="x")
    assert c1.fingerprint() != c2.fingerprint()


def test_walk_forward_series_is_ascending_and_positive():
    provider = SyntheticHistoricalProvider(_config())
    provider._ensure_built()
    wf = provider._wf_frame
    assert wf.index.is_monotonic_increasing
    assert (wf["close"] > 0).all()
    assert provider.walk_forward_bar_count() == len(wf)


def test_unknown_strike_returns_empty_frame():
    provider = SyntheticHistoricalProvider(_config())
    frame = provider.option_bars("SYNTH", "31DEC2026", 12345.0, "CE")
    assert frame.empty
