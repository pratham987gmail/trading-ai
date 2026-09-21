"""Tests for purge-and-embargo index arithmetic (spec section 18)."""

from __future__ import annotations

import pytest

from research.backtest.purged_embargo import purge_and_embargo


class TestWorkedExample:
    """The exact worked example from the module's own docstring."""

    def test_purged_indices_are_90_to_99(self) -> None:
        result = purge_and_embargo(
            total_bars=200,
            train_range=(0, 100),
            test_range=(100, 150),
            label_horizon_bars=10,
            embargo_bars=5,
        )
        assert result["purged_indices"] == list(range(90, 100))

    def test_index_89_survives_purging(self) -> None:
        result = purge_and_embargo(
            total_bars=200,
            train_range=(0, 100),
            test_range=(100, 150),
            label_horizon_bars=10,
            embargo_bars=5,
        )
        assert 89 in result["train_indices"]
        assert 89 not in result["purged_indices"]

    def test_embargo_indices_150_to_154_when_train_range_extends_past_test(self) -> None:
        # Extend train_range past the test period so the embargo window
        # actually has candidates to remove from.
        result = purge_and_embargo(
            total_bars=200,
            train_range=(0, 160),
            test_range=(100, 150),
            label_horizon_bars=10,
            embargo_bars=5,
        )
        assert result["embargo_indices"] == list(range(150, 155))

    def test_narrow_train_range_produces_empty_embargo(self) -> None:
        result = purge_and_embargo(
            total_bars=200,
            train_range=(0, 100),
            test_range=(100, 150),
            label_horizon_bars=10,
            embargo_bars=5,
        )
        assert result["embargo_indices"] == []


class TestBoundaryPrecision:
    def test_label_horizon_zero_only_purges_indices_inside_test_range(self) -> None:
        result = purge_and_embargo(
            total_bars=100,
            train_range=(0, 60),
            test_range=(50, 70),
            label_horizon_bars=0,
            embargo_bars=0,
        )
        # With horizon 0, a label at i only uses info at i itself, so only
        # indices already inside [test_start, test_end) are purged.
        assert result["purged_indices"] == list(range(50, 60))

    def test_zero_embargo_removes_nothing_extra(self) -> None:
        result = purge_and_embargo(
            total_bars=200,
            train_range=(0, 160),
            test_range=(100, 150),
            label_horizon_bars=10,
            embargo_bars=0,
        )
        assert result["embargo_indices"] == []


class TestDisjointCoverage:
    """Property-style: purged, embargo, and remaining train partition train_range exactly."""

    @pytest.mark.parametrize(
        "total_bars,train_range,test_range,label_horizon_bars,embargo_bars",
        [
            (200, (0, 160), (100, 150), 10, 5),
            (300, (0, 250), (100, 150), 20, 10),
            (100, (0, 100), (40, 60), 5, 3),
            (50, (0, 50), (0, 10), 3, 2),
            (500, (200, 450), (300, 350), 15, 20),
        ],
    )
    def test_partition_covers_full_train_range_without_overlap(
        self, total_bars, train_range, test_range, label_horizon_bars, embargo_bars
    ) -> None:
        result = purge_and_embargo(
            total_bars=total_bars,
            train_range=train_range,
            test_range=test_range,
            label_horizon_bars=label_horizon_bars,
            embargo_bars=embargo_bars,
        )
        train_set = set(result["train_indices"])
        purged_set = set(result["purged_indices"])
        embargo_set = set(result["embargo_indices"])

        # Pairwise disjoint.
        assert train_set.isdisjoint(purged_set)
        assert train_set.isdisjoint(embargo_set)
        assert purged_set.isdisjoint(embargo_set)

        # Union covers exactly the original candidate train range.
        expected_full = set(range(train_range[0], train_range[1]))
        assert train_set | purged_set | embargo_set == expected_full


class TestValidation:
    def test_negative_label_horizon_raises(self) -> None:
        with pytest.raises(ValueError):
            purge_and_embargo(
                total_bars=100, train_range=(0, 50), test_range=(50, 70),
                label_horizon_bars=-1,
            )

    def test_negative_embargo_raises(self) -> None:
        with pytest.raises(ValueError):
            purge_and_embargo(
                total_bars=100, train_range=(0, 50), test_range=(50, 70),
                label_horizon_bars=5, embargo_bars=-1,
            )

    def test_inverted_train_range_raises(self) -> None:
        with pytest.raises(ValueError):
            purge_and_embargo(
                total_bars=100, train_range=(50, 10), test_range=(60, 70),
                label_horizon_bars=5,
            )

    def test_out_of_bounds_range_raises(self) -> None:
        with pytest.raises(ValueError):
            purge_and_embargo(
                total_bars=100, train_range=(0, 50), test_range=(60, 200),
                label_horizon_bars=5,
            )
