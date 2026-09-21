"""Purging and embargo for future-return-labeled datasets (spec section 18).

When a training example's label is computed from a forward-looking window
(e.g. "was the 10-bar-ahead return positive"), that example leaks information
about the future into training. If part of that forward window falls inside
the test period, the training example was partly labeled using data that is
concurrent with — or drawn from — the period the model is later evaluated
against. Cross-validating on that example overstates out-of-sample skill.

**Purging** removes any training index whose label window overlaps the test
range at all. **Embargo** additionally removes a buffer of training indices
immediately after the test period, because labels (and the market states that
produce them) are autocorrelated: a training example a few bars past the test
boundary is not truly independent of it even though its label window does not
literally overlap.

This module is pure index arithmetic. No I/O, no external calls, no
knowledge of what the labels actually are.
"""

from __future__ import annotations

from typing import Any


def purge_and_embargo(
    total_bars: int,
    train_range: tuple[int, int],
    test_range: tuple[int, int],
    label_horizon_bars: int,
    embargo_bars: int = 0,
) -> dict[str, Any]:
    """Purge and embargo a training range against a test range.

    Args:
        total_bars: Total bars in the dataset. Indices ``0..total_bars-1`` are
            valid.
        train_range: ``(start, end)``, exclusive of ``end``, of the candidate
            training indices before purging.
        test_range: ``(start, end)``, exclusive of ``end``, of the test
            indices.
        label_horizon_bars: How many bars forward a label looks. An example
            labeled at index ``i`` uses information through bar
            ``i + label_horizon_bars`` inclusive (e.g. a return-over-the-
            next-N-bars label is computed from the close N bars ahead).
            Must be non-negative.
        embargo_bars: Additional training indices to drop immediately after
            the test period, in ``[test_end, test_end + embargo_bars)``. Must
            be non-negative.

    Returns:
        A dict with:

        - ``train_indices``: surviving training indices (sorted, disjoint
          from ``purged_indices`` and ``embargo_indices``).
        - ``purged_indices``: training indices dropped because their label
          window overlaps the test range.
        - ``embargo_indices``: training indices dropped by the post-test
          embargo buffer (and not already purged).
        - ``notes``: human-readable explanation of what happened.

    Raises:
        ValueError: If ``label_horizon_bars`` or ``embargo_bars`` is
            negative, or if either range is inverted or out of
            ``[0, total_bars]``.

    Worked example (matches the module's test suite):
        ``total_bars=200``, ``train_range=(0, 100)``, ``test_range=(100,
        150)``, ``label_horizon_bars=10``, ``embargo_bars=5``.

        A training index ``i``'s label reaches forward to the price at bar
        ``i + label_horizon_bars`` (a "return over the next 10 bars" label
        uses the close 10 bars ahead as its target, so it consumes
        information through — inclusive of — bar ``i + 10``, not just up to
        it). That reach touches the test range ``[100, 150)`` whenever
        ``i + 10 >= 100``, i.e. ``i >= 90``. So indices ``90..99`` (the
        portion of ``[0, 100)`` satisfying that) are purged: index 90's
        label reaches bar 100, which is the first bar of the test range —
        genuine leakage, not a near-miss. Index 89's label reaches only bar
        99, strictly before the test range, and survives.

        The embargo then drops ``[150, 155)`` — indices 150..154 — from the
        training candidates. None of those indices are in this particular
        ``train_range`` (which ends at 100), so in this example
        ``embargo_indices`` is empty; embargo only ever removes indices that
        are both in ``train_range`` and in the embargo window, which
        requires a training range extending past the test period, as tested
        directly in ``test_purged_embargo.py`` with a wider train range.
    """
    if label_horizon_bars < 0:
        raise ValueError("label_horizon_bars must be non-negative")
    if embargo_bars < 0:
        raise ValueError("embargo_bars must be non-negative")

    train_start, train_end = train_range
    test_start, test_end = test_range

    for name, start, end in (("train_range", train_start, train_end), ("test_range", test_start, test_end)):
        if start > end:
            raise ValueError(f"{name} is inverted: start {start} > end {end}")
        if start < 0 or end > total_bars:
            raise ValueError(
                f"{name}=({start}, {end}) falls outside valid bounds [0, {total_bars}]"
            )

    candidate_train = set(range(train_start, train_end))

    # A training index i's label reaches forward to (and uses, inclusive of)
    # bar i + label_horizon_bars -- e.g. a "return over the next 10 bars"
    # label is computed from the close 10 bars ahead. That reach touches the
    # test range [test_start, test_end) iff i is before the test range ends
    # (i < test_end) and the reach lands at or past the test start
    # (i + label_horizon_bars >= test_start).
    purged = {
        i
        for i in candidate_train
        if i < test_end and (i + label_horizon_bars) >= test_start
    }

    remaining_after_purge = candidate_train - purged

    embargo_window = set(range(test_end, min(test_end + embargo_bars, total_bars)))
    embargo = remaining_after_purge & embargo_window

    train_indices = sorted(remaining_after_purge - embargo)

    notes = [
        f"{len(purged)} training index(es) purged: their label reaches "
        f"forward {label_horizon_bars} bar(s), landing at or past the test "
        f"range [{test_start}, {test_end}).",
        f"{len(embargo)} training index(es) embargoed: within "
        f"[{test_end}, {test_end + embargo_bars}) immediately after the test range.",
        f"{len(train_indices)} training index(es) remain out of "
        f"{len(candidate_train)} originally in train_range.",
    ]

    return {
        "train_indices": train_indices,
        "purged_indices": sorted(purged),
        "embargo_indices": sorted(embargo),
        "notes": notes,
    }
