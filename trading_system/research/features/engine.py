"""Leakage-safe feature computation over OHLCV (+ optional options/regime) frames.

**The single rule this module exists to enforce:** a feature's value at bar
``i`` must be a function of ``frame.iloc[:i+1]`` only — never of any bar after
``i``. That is what makes a feature usable for training a model that will
later predict on live, incomplete data: if computing it required seeing bar
``i+3``, the model would learn a relationship that cannot exist at prediction
time.

Two methods enforce this from different directions:

- :meth:`FeatureEngine.compute_frame` computes every feature vectorised over
  the whole frame using only ``.rolling()``, ``.shift()``, and
  ``.expanding()`` — never ``center=True``, never a negative shift, never
  ``.bfill()``. This is the fast, leakage-critical path.
- :meth:`FeatureEngine.compute` computes a single bar's feature vector by
  **truncating the frame to that bar first** (``frame.iloc[:as_of_index+1]``)
  and then calling :meth:`compute_frame` on the truncated frame, taking its
  last row. This is not merely consistent with ``compute_frame`` by
  construction — it makes leakage structurally impossible for this method,
  since no code here ever has access to a row after ``as_of_index`` in the
  first place. ``tests/research/test_feature_leakage.py`` checks both
  directions: that ``compute`` matches ``compute_frame`` row-for-row, and
  that corrupting a future row never changes an earlier row's output.

Feature identity, lookback, and the ``uses_future_data`` guarantee are
declared in :mod:`research.features.provenance` before any of this file
computes a number — see that module's docstring for why the split matters.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from analysis import indicators
from research.features.provenance import FEATURE_REGISTRY

#: Option-specific columns the engine treats as optional. Absent entirely ->
#: the whole "options" group is skipped, not fabricated.
_OPTION_COLUMNS = ("open_interest", "iv", "delta", "gamma", "theta", "vega", "bid", "ask")


class FeatureEngine:
    """Computes the registered feature set over an OHLCV(+options/regime) frame.

    Args:
        None. Stateless aside from ``last_report``, which is overwritten by
        every call to :meth:`compute_frame` (and, transitively,
        :meth:`compute`) so a caller can see which features were actually
        computable for the frame just processed versus skipped for missing
        columns.
    """

    def __init__(self) -> None:
        #: ``{feature_name: "computed" | "skipped: <reason>"}`` from the most
        #: recent :meth:`compute_frame` call. Populated fresh every call.
        self.last_report: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_frame(
        self,
        frame: pd.DataFrame,
        *,
        expiry_date: date | datetime | str | None = None,
    ) -> pd.DataFrame:
        """Compute every registered feature for every bar, vectorised.

        Args:
            frame: OHLCV frame (``open``/``high``/``low``/``close``/``volume``
                columns, ``DatetimeIndex`` for VWAP and time features).
                Optionally carries option columns
                (``open_interest``/``iv``/``delta``/``gamma``/``theta``/
                ``vega``/``bid``/``ask``) and a ``regime`` string column;
                each group is skipped gracefully (NaN-filled, never
                fabricated) when its source columns are absent.
            expiry_date: Only used for ``time_to_expiry_days``; each bar's
                own timestamp is compared against this fixed date, never
                against another bar.

        Returns:
            A frame with one column per entry in
            :data:`research.features.provenance.FEATURE_REGISTRY`, aligned to
            ``frame``'s index. ``NaN`` during warmup or wherever a required
            source column is missing — never a fabricated number.
        """
        columns: dict[str, pd.Series] = {}
        report: dict[str, str] = {}

        def emit(name: str, series: pd.Series, note: str = "computed") -> None:
            columns[name] = series.reindex(frame.index)
            report[name] = note

        def skip(names: tuple[str, ...] | list[str], reason: str) -> None:
            for name in names:
                columns[name] = pd.Series(np.nan, index=frame.index, dtype=float)
                report[name] = f"skipped: {reason}"

        if frame.empty:
            empty = pd.DataFrame(
                {name: pd.Series(dtype=float) for name in FEATURE_REGISTRY}, index=frame.index
            )
            self.last_report = {name: "skipped: empty frame" for name in FEATURE_REGISTRY}
            return empty

        has_ohlc = all(c in frame.columns for c in ("open", "high", "low", "close"))
        has_volume = "volume" in frame.columns
        has_datetime_index = isinstance(frame.index, pd.DatetimeIndex)

        # ---------------- Price ----------------
        if has_ohlc:
            close = frame["close"].astype(float)
            open_ = frame["open"].astype(float)
            high = frame["high"].astype(float)
            low = frame["low"].astype(float)

            emit("returns", close.pct_change())
            emit("log_returns", np.log(close / close.shift(1)))
            emit("momentum_10", indicators.rate_of_change(close, period=10))
            emit("high_low_range", high - low)
            emit("gap", open_ - close.shift(1))
            emit("dist_from_prev_high", close - high.shift(1))
            emit("dist_from_prev_low", close - low.shift(1))
        else:
            skip(
                ("returns", "log_returns", "momentum_10", "high_low_range", "gap",
                 "dist_from_prev_high", "dist_from_prev_low"),
                "missing open/high/low/close columns",
            )

        # ---------------- Technical ----------------
        if has_ohlc:
            emit("rsi_14", indicators.rsi(frame, period=14))
            macd_result = indicators.macd(frame)
            emit("macd", macd_result.macd)
            emit("macd_signal", macd_result.signal)
            emit("macd_hist", macd_result.histogram)
            emit("atr_14", indicators.atr(frame, period=14))
            adx_result = indicators.adx(frame, period=14)
            emit("adx_14", adx_result.adx)
            bands = indicators.bollinger_bands(frame, period=20)
            emit("bb_upper", bands.upper)
            emit("bb_lower", bands.lower)
            emit("bb_width_pct", bands.width_pct())
        else:
            skip(
                ("rsi_14", "macd", "macd_signal", "macd_hist", "atr_14", "adx_14",
                 "bb_upper", "bb_lower", "bb_width_pct"),
                "missing open/high/low/close columns",
            )

        if has_ohlc and has_volume and has_datetime_index:
            vwap_series = indicators.vwap(frame, reset_daily=True)
            emit("vwap", vwap_series)
            emit("vwap_deviation", (frame["close"].astype(float) - vwap_series) / vwap_series.replace(0.0, np.nan))
        else:
            skip(
                ("vwap", "vwap_deviation"),
                "vwap needs high/low/close/volume and a DatetimeIndex",
            )

        # ---------------- Volume ----------------
        if has_volume:
            volume = frame["volume"].astype(float)
            emit("volume", volume)
            avg_20 = volume.rolling(20, min_periods=20).mean()
            emit("relative_volume", volume / avg_20.replace(0.0, np.nan))
            emit("volume_percentile", _rolling_percentile(volume, 100))
            emit("volume_acceleration", indicators.rate_of_change(volume, period=1))
        else:
            skip(
                ("volume", "relative_volume", "volume_percentile", "volume_acceleration"),
                "missing volume column",
            )

        # ---------------- Volatility ----------------
        if has_ohlc:
            close = frame["close"].astype(float)
            returns = close.pct_change()
            emit("realized_volatility", returns.rolling(20, min_periods=20).std(ddof=0))

            atr_series = indicators.atr(frame, period=14)
            emit("atr_percentile", _rolling_percentile(atr_series, 100))

            true_range = indicators.true_range(frame)
            atr_known = atr_series.notna()
            expansion = (true_range > 1.5 * atr_series).astype(float)
            compression = (true_range < 0.5 * atr_series).astype(float)
            expansion[~atr_known] = np.nan
            compression[~atr_known] = np.nan
            emit("range_expansion", expansion)
            emit("range_compression", compression)
        else:
            skip(
                ("realized_volatility", "atr_percentile", "range_expansion", "range_compression"),
                "missing open/high/low/close columns",
            )

        # ---------------- Options (optional) ----------------
        if "open_interest" in frame.columns:
            oi = frame["open_interest"].astype(float)
            emit("oi", oi)
            emit("oi_change", oi.diff())
        else:
            skip(("oi", "oi_change"), "missing open_interest column")

        # PCR needs both CE and PE legs simultaneously, which a single-leg
        # frame never carries. Always skipped at this per-leg engine level —
        # see provenance.py's note on this feature.
        skip(("pcr",), "PCR requires both CE and PE legs; not derivable from a single-leg frame")

        if "iv" in frame.columns:
            iv = frame["iv"].astype(float)
            emit("iv", iv)
            emit("iv_change", iv.diff())
        else:
            skip(("iv", "iv_change"), "missing iv column")

        for greek in ("delta", "gamma", "theta", "vega"):
            if greek in frame.columns:
                emit(greek, frame[greek].astype(float))
            else:
                skip((greek,), f"missing {greek} column")

        if "bid" in frame.columns and "ask" in frame.columns:
            emit("bid_ask_spread", frame["ask"].astype(float) - frame["bid"].astype(float))
        else:
            skip(("bid_ask_spread",), "missing bid and/or ask column")

        # ---------------- Time ----------------
        if has_datetime_index:
            emit(
                "minute_of_day",
                pd.Series(frame.index.hour * 60 + frame.index.minute, index=frame.index, dtype=float),
            )
            emit("day_of_week", pd.Series(frame.index.dayofweek, index=frame.index, dtype=float))
        else:
            skip(("minute_of_day", "day_of_week"), "no DatetimeIndex")

        if has_datetime_index and expiry_date is not None:
            expiry = _to_date(expiry_date)
            days = pd.Series(
                [(expiry - ts.date()).days for ts in frame.index], index=frame.index, dtype=float
            )
            emit("time_to_expiry_days", days)
        else:
            skip(
                ("time_to_expiry_days",),
                "no DatetimeIndex" if not has_datetime_index else "no expiry_date given",
            )

        # ---------------- Regime (optional) ----------------
        if "regime" in frame.columns:
            regime = frame["regime"]
            emit("regime", regime)
            emit("regime_is_known", regime.notna().astype(float))
        else:
            skip(("regime", "regime_is_known"), "missing regime column")

        result = pd.DataFrame(columns, index=frame.index)
        # Emit every registered feature in registry order, even if some group
        # above never touched it (defensive: keeps the frame's columns a
        # strict match to FEATURE_REGISTRY regardless of branch changes).
        for name in FEATURE_REGISTRY:
            if name not in result.columns:
                result[name] = np.nan
                report.setdefault(name, "skipped: not computed")
        result = result[list(FEATURE_REGISTRY.keys())]

        self.last_report = report
        return result

    def compute(
        self,
        frame: pd.DataFrame,
        *,
        as_of_index: int | None = None,
        expiry_date: date | datetime | str | None = None,
    ) -> pd.Series:
        """Compute the feature vector for a single bar.

        Truncates ``frame`` to ``frame.iloc[:as_of_index + 1]`` **before**
        computing anything, then delegates to :meth:`compute_frame` and
        returns its last row. This is deliberate, not an optimisation detail:
        it means no code path in this method ever holds a reference to a bar
        after ``as_of_index``, so there is nothing for a bug elsewhere in the
        engine to accidentally leak.

        Args:
            frame: OHLCV(+options/regime) frame.
            as_of_index: Positional index of the bar to compute features for.
                Defaults to the last bar. Must be a valid position in
                ``frame`` (``0 <= as_of_index < len(frame)``).
            expiry_date: Passed through to :meth:`compute_frame`.

        Returns:
            A single-row feature vector (one entry per registered feature),
            indexed by feature name.

        Raises:
            ValueError: When ``frame`` is empty or ``as_of_index`` is out of
                range.
        """
        if frame.empty:
            raise ValueError("Cannot compute features on an empty frame")

        if as_of_index is None:
            as_of_index = len(frame) - 1
        if not 0 <= as_of_index < len(frame):
            raise ValueError(
                f"as_of_index {as_of_index} out of range for frame of length {len(frame)}"
            )

        truncated = frame.iloc[: as_of_index + 1]
        computed = self.compute_frame(truncated, expiry_date=expiry_date)
        return computed.iloc[-1]


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """Rank of the current value within its trailing window, as a fraction.

    Uses only ``series.iloc[i - window + 1 : i + 1]`` for row ``i`` — the
    window always ends at (and includes) the current bar, never extends past
    it. ``NaN`` until a full window exists.

    Args:
        series: Input values.
        window: Trailing window length.

    Returns:
        Fraction in ``[0, 1]``: the share of the trailing window at or below
        the current value.
    """

    def _pct_rank(values: np.ndarray) -> float:
        current = values[-1]
        if np.isnan(current):
            return np.nan
        finite = values[~np.isnan(values)]
        if len(finite) == 0:
            return np.nan
        return float((finite <= current).sum()) / len(finite)

    return series.rolling(window, min_periods=window).apply(_pct_rank, raw=True)


def _to_date(value: date | datetime | str) -> date:
    """Coerce an expiry input to a plain ``date`` for arithmetic against a bar's own date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()
