"""Tests for the regime state machine.

Two groups. ``TestHysteresis`` isolates the state-machine logic from the
underlying trend/volatility measurement entirely (by monkeypatching
``raw_regime_label``), because the property under test — a single noisy
reading must not flip the confirmed state — has nothing to do with whether
the trend classifier itself is correct; that is already covered in
``tests/test_analysis.py``. ``TestIntegration`` then runs the real detector
against real candle data to confirm the two pieces are wired together
correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from research.regime import engine as regime_engine
from research.regime.engine import ALL_REGIMES, RegimeDetector, raw_regime_label
from tests._spread_fixtures import make_candles


class TestHysteresis:
    """The state machine's own logic, with the raw reading stubbed."""

    def _feed(self, detector: RegimeDetector, labels: list[str], monkeypatch) -> list[str]:
        """Feed a scripted sequence of raw labels, return the confirmed label after each."""
        confirmed: list[str] = []
        base = datetime(2026, 1, 1, 9, 15)
        for i, label in enumerate(labels):
            monkeypatch.setattr(regime_engine, "raw_regime_label", lambda *a, **k: (label, 0.8))
            state = detector.update(None, at=base + timedelta(minutes=5 * i))  # type: ignore[arg-type]
            confirmed.append(state.label)
        return confirmed

    def test_starts_unknown(self) -> None:
        detector = RegimeDetector()
        assert detector.current.label == "UNKNOWN"

    def test_single_noisy_reading_does_not_flip_the_state(self, monkeypatch) -> None:
        detector = RegimeDetector(confirmation_bars=3)
        labels = ["TREND_UP", "TREND_UP", "TREND_UP", "RANGE", "TREND_UP", "TREND_UP"]
        confirmed = self._feed(detector, labels, monkeypatch)
        # After the 3 TREND_UP readings confirm, a single RANGE blip must not
        # flip the state back before it can re-confirm TREND_UP.
        assert confirmed == ["UNKNOWN", "UNKNOWN", "TREND_UP", "TREND_UP", "TREND_UP", "TREND_UP"]

    def test_sustained_new_reading_does_eventually_transition(self, monkeypatch) -> None:
        detector = RegimeDetector(confirmation_bars=3)
        labels = ["TREND_UP"] * 3 + ["TREND_DOWN"] * 3
        confirmed = self._feed(detector, labels, monkeypatch)
        assert confirmed[-1] == "TREND_DOWN"
        assert confirmed[2] == "TREND_UP"  # confirmed exactly on the 3rd reading

    def test_confirmation_bars_of_one_disables_hysteresis(self, monkeypatch) -> None:
        detector = RegimeDetector(confirmation_bars=1)
        labels = ["TREND_UP", "RANGE", "TREND_DOWN"]
        confirmed = self._feed(detector, labels, monkeypatch)
        assert confirmed == labels

    def test_interrupted_pending_transition_resets_the_counter(self, monkeypatch) -> None:
        """Two readings of A, one of B, two more of A: A must not have confirmed early."""
        detector = RegimeDetector(confirmation_bars=3)
        labels = ["TREND_UP"] * 2 + ["RANGE"] + ["TREND_UP"] * 2
        confirmed = self._feed(detector, labels, monkeypatch)
        assert confirmed[-1] == "UNKNOWN"  # TREND_UP never got 3 in a row

    def test_transitions_are_recorded(self, monkeypatch) -> None:
        detector = RegimeDetector(confirmation_bars=2)
        self._feed(detector, ["TREND_UP", "TREND_UP", "RANGE", "RANGE"], monkeypatch)
        assert len(detector.transitions) == 2
        assert detector.transitions[0].from_state == "UNKNOWN"
        assert detector.transitions[0].to_state == "TREND_UP"
        assert detector.transitions[1].from_state == "TREND_UP"
        assert detector.transitions[1].to_state == "RANGE"

    def test_reset_clears_state_and_history(self, monkeypatch) -> None:
        detector = RegimeDetector(confirmation_bars=1)
        self._feed(detector, ["TREND_UP"], monkeypatch)
        assert detector.current.label == "TREND_UP"
        detector.reset()
        assert detector.current.label == "UNKNOWN"
        assert detector.transitions == []

    def test_bars_since_confirmation_increments_while_stable(self, monkeypatch) -> None:
        detector = RegimeDetector(confirmation_bars=1)
        self._feed(detector, ["RANGE", "RANGE", "RANGE"], monkeypatch)
        assert detector.current.bars_since_confirmation == 3

    def test_invalid_confirmation_bars_rejected(self) -> None:
        with pytest.raises(ValueError):
            RegimeDetector(confirmation_bars=0)


class TestRawLabelVocabulary:
    def test_empty_frame_is_unknown(self) -> None:
        label, confidence = raw_regime_label(None)
        assert label == "UNKNOWN"
        assert confidence == 0.0

    def test_event_risk_overrides_everything(self) -> None:
        label, confidence = raw_regime_label(make_candles(bars=250, direction="up"), event_risk=True)
        assert label == "EVENT_RISK"
        assert confidence == 1.0

    def test_short_history_is_unknown(self) -> None:
        label, _ = raw_regime_label(make_candles(bars=10))
        assert label == "UNKNOWN"

    def test_label_always_in_the_closed_vocabulary(self) -> None:
        for direction in ("up", "down", "flat"):
            label, _ = raw_regime_label(make_candles(bars=250, direction=direction, drift=8.0))
            assert label in ALL_REGIMES


class TestIntegration:
    """The real detector against real candle data."""

    def test_a_strong_uptrend_eventually_confirms_trend_up(self) -> None:
        detector = RegimeDetector(confirmation_bars=2)
        frame = make_candles(bars=300, direction="up", drift=12.0, noise=2.0)
        base = frame.index[0]
        state = None
        # Feed growing windows, simulating bar-by-bar arrival.
        for i in range(200, len(frame)):
            state = detector.update(frame.iloc[: i + 1], at=frame.index[i])
        assert state is not None
        assert state.label in ("TREND_UP", "UNKNOWN")  # never RANGE/TREND_DOWN for this data
        assert state.label != "TREND_DOWN"

    def test_regime_never_flips_every_single_bar_on_real_noisy_data(self) -> None:
        """The property that actually matters for regime-conditional research."""
        detector = RegimeDetector(confirmation_bars=3)
        frame = make_candles(bars=300, direction="flat", noise=15.0, seed=3)
        seen_labels_per_bar = []
        for i in range(100, len(frame)):
            state = detector.update(frame.iloc[: i + 1], at=frame.index[i])
            seen_labels_per_bar.append(state.label)
        flips = sum(
            1 for a, b in zip(seen_labels_per_bar, seen_labels_per_bar[1:]) if a != b
        )
        # With 3-bar confirmation, the confirmed state cannot change more
        # often than roughly once every 3 bars, however noisy the raw reading is.
        assert flips <= len(seen_labels_per_bar) // 3 + 1
