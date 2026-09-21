"""Historical OHLCV loading and quality validation.

Backtest conclusions inherit the quality of their input, and bad bars fail
quietly: one impossible high inflates a stop-hit rate, a silent gap turns an
overnight move into an intraday one. So validation runs by default and reports
what it found rather than silently repairing it.

The distinction maintained throughout: **flag and report, do not fabricate**.
Missing bars are recorded as gaps, not forward-filled into invented prices. A
strategy tested on interpolated data is tested on fiction.

Example:
    >>> from backtest.data_loader import load_csv, validate_ohlcv
    >>> frame = load_csv("nifty_5min.csv")            # doctest: +SKIP
    >>> report = validate_ohlcv(frame)                # doctest: +SKIP
    >>> report.is_usable                              # doctest: +SKIP
    True
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("open", "high", "low", "close")
OPTIONAL_COLUMNS = ("volume", "open_interest")

# Common header spellings mapped to the canonical names.
COLUMN_ALIASES: dict[str, str] = {
    "date": "timestamp",
    "datetime": "timestamp",
    "time": "timestamp",
    "ts": "timestamp",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "vol": "volume",
    "oi": "open_interest",
    "openinterest": "open_interest",
    "adj close": "close",
    "adj_close": "close",
}

NSE_SESSION_START = time(9, 15)
NSE_SESSION_END = time(15, 30)


@dataclass
class ValidationReport:
    """Outcome of a data quality check.

    Attributes:
        rows: Row count after loading.
        start: First timestamp.
        end: Last timestamp.
        errors: Problems that make the data unsafe to backtest on.
        warnings: Problems worth knowing that do not invalidate the run.
        stats: Descriptive statistics.
        gaps: Detected gaps in the expected bar sequence.
    """

    rows: int = 0
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    gaps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """True when no blocking errors were found."""
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """Return the report as a plain mapping."""
        return {
            "rows": self.rows,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "is_usable": self.is_usable,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "gap_count": len(self.gaps),
            "gaps": self.gaps[:20],
            "stats": dict(self.stats),
        }

    def summary(self) -> str:
        """Return a human-readable summary."""
        lines = [
            f"Rows: {self.rows}",
            f"Range: {self.start} to {self.end}",
            f"Usable: {'yes' if self.is_usable else 'NO'}",
        ]
        if self.errors:
            lines.append(f"Errors ({len(self.errors)}):")
            lines += [f"  - {e}" for e in self.errors]
        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            lines += [f"  - {w}" for w in self.warnings[:10]]
            if len(self.warnings) > 10:
                lines.append(f"  ... and {len(self.warnings) - 10} more")
        return "\n".join(lines)


def normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce an arbitrary OHLCV frame into the canonical shape.

    Lowercases and de-aliases headers, parses the timestamp into a
    ``DatetimeIndex``, coerces numerics, sorts, and drops duplicate timestamps
    keeping the last.

    Args:
        frame: Raw frame.

    Returns:
        Frame indexed by timestamp with numeric OHLCV columns.

    Raises:
        ValueError: If required columns or a usable timestamp are missing.
    """
    result = frame.copy()
    result.columns = [str(c).strip().lower() for c in result.columns]
    result = result.rename(columns={k: v for k, v in COLUMN_ALIASES.items() if k in result.columns})

    if "timestamp" not in result.columns:
        if isinstance(result.index, pd.DatetimeIndex):
            result = result.reset_index().rename(
                columns={result.index.name or "index": "timestamp"}
            )
            result.columns = [str(c).strip().lower() for c in result.columns]
        else:
            raise ValueError(
                "No timestamp column found. Expected one of: timestamp, date, datetime, time."
            )

    missing = [c for c in REQUIRED_COLUMNS if c not in result.columns]
    if missing:
        raise ValueError(
            f"Missing required columns: {', '.join(missing)}. "
            f"Found: {', '.join(result.columns)}"
        )

    result["timestamp"] = pd.to_datetime(result["timestamp"], errors="coerce")
    dropped = int(result["timestamp"].isna().sum())
    if dropped:
        logger.warning("Dropped %d rows with unparseable timestamps", dropped)
        result = result.dropna(subset=["timestamp"])

    for column in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")

    result = result.set_index("timestamp").sort_index()

    duplicates = int(result.index.duplicated().sum())
    if duplicates:
        logger.warning("Dropped %d duplicate timestamps, keeping the last", duplicates)
        result = result[~result.index.duplicated(keep="last")]

    keep = [c for c in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS) if c in result.columns]
    return result[keep]


def validate_ohlcv(
    frame: pd.DataFrame,
    expected_interval_minutes: int | None = None,
    session_only: bool = True,
) -> ValidationReport:
    """Validate an OHLCV frame.

    Checks structural invariants that must hold for any real bar, then reports
    gaps and distributional oddities as warnings.

    Args:
        frame: Normalised OHLCV frame.
        expected_interval_minutes: Bar interval, for gap detection. Inferred from
            the modal timestamp difference when omitted.
        session_only: Treat bars outside 09:15-15:30 IST as suspicious.

    Returns:
        A :class:`ValidationReport`.
    """
    report = ValidationReport(rows=len(frame))

    if frame.empty:
        report.errors.append("Frame is empty")
        return report

    report.start = frame.index[0]
    report.end = frame.index[-1]

    # -- structural invariants ------------------------------------------
    for column in REQUIRED_COLUMNS:
        nulls = int(frame[column].isna().sum())
        if nulls:
            report.errors.append(f"{column} has {nulls} missing values")

    complete = frame.dropna(subset=list(REQUIRED_COLUMNS))
    if complete.empty:
        report.errors.append("No rows have complete OHLC data")
        return report

    high_below_low = int((complete["high"] < complete["low"]).sum())
    if high_below_low:
        report.errors.append(f"{high_below_low} bars have high < low, which is impossible")

    high_violations = int(
        ((complete["high"] < complete["open"]) | (complete["high"] < complete["close"])).sum()
    )
    if high_violations:
        report.errors.append(
            f"{high_violations} bars have high below open or close, which is impossible"
        )

    low_violations = int(
        ((complete["low"] > complete["open"]) | (complete["low"] > complete["close"])).sum()
    )
    if low_violations:
        report.errors.append(
            f"{low_violations} bars have low above open or close, which is impossible"
        )

    non_positive = int((complete[list(REQUIRED_COLUMNS)] <= 0).any(axis=1).sum())
    if non_positive:
        report.errors.append(f"{non_positive} bars have a non-positive price")

    if "volume" in complete.columns:
        negative_volume = int((complete["volume"] < 0).sum())
        if negative_volume:
            report.errors.append(f"{negative_volume} bars have negative volume")
        zero_volume = int((complete["volume"] == 0).sum())
        if zero_volume:
            report.warnings.append(
                f"{zero_volume} bars have zero volume ({zero_volume / len(complete):.1%}); "
                "fills modelled in those bars are unrealistic"
            )

    # -- interval and gaps ----------------------------------------------
    deltas = frame.index.to_series().diff().dropna()
    interval = expected_interval_minutes
    if interval is None and not deltas.empty:
        modal = deltas.mode()
        if not modal.empty:
            interval = max(1, int(modal.iloc[0].total_seconds() // 60))

    if interval:
        report.stats["interval_minutes"] = interval
        expected = pd.Timedelta(minutes=interval)
        # Overnight and weekend boundaries are legitimate, so only flag
        # within-session discontinuities.
        for stamp, delta in deltas.items():
            if delta <= expected:
                continue
            previous = stamp - delta
            same_day = previous.date() == stamp.date()
            if not same_day:
                continue
            missing = int(delta / expected) - 1
            if missing > 0:
                report.gaps.append(
                    {
                        "after": previous.isoformat(),
                        "before": stamp.isoformat(),
                        "missing_bars": missing,
                        "gap_minutes": int(delta.total_seconds() // 60),
                    }
                )

        if report.gaps:
            total_missing = sum(g["missing_bars"] for g in report.gaps)
            report.warnings.append(
                f"{len(report.gaps)} intraday gaps totalling {total_missing} missing bars. "
                "Gaps are reported, not filled: interpolated prices would be fabricated data."
            )

    # -- session bounds -------------------------------------------------
    if session_only:
        times = frame.index.time
        outside = int(sum(1 for t in times if t < NSE_SESSION_START or t > NSE_SESSION_END))
        if outside:
            report.warnings.append(
                f"{outside} bars fall outside 09:15-15:30 IST. Check the timezone of "
                "the source data; a UTC-labelled series will shift every bar."
            )

    # -- distribution ---------------------------------------------------
    returns = complete["close"].pct_change().dropna()
    if not returns.empty:
        extreme = int((returns.abs() > 0.10).sum())
        if extreme:
            report.warnings.append(
                f"{extreme} bars move more than 10%. Plausible for options, "
                "suspicious for an index at intraday resolution."
            )
        report.stats["return_std"] = round(float(returns.std()), 6)
        report.stats["max_abs_return"] = round(float(returns.abs().max()), 6)

    trading_days = len({ts.date() for ts in frame.index})
    report.stats.update(
        {
            "trading_days": trading_days,
            "bars_per_day": round(len(frame) / trading_days, 1) if trading_days else 0,
            "first_bar": report.start.isoformat(),
            "last_bar": report.end.isoformat(),
            "close_min": round(float(complete["close"].min()), 2),
            "close_max": round(float(complete["close"].max()), 2),
        }
    )

    if trading_days < 20:
        report.warnings.append(
            f"Only {trading_days} trading days. Too short for a meaningful backtest; "
            "the charter requires 100+ trades for statistical significance."
        )

    return report


def load_csv(
    path: str | Path,
    validate: bool = True,
    expected_interval_minutes: int | None = None,
) -> pd.DataFrame:
    """Load OHLCV data from CSV.

    Args:
        path: Path to the CSV file.
        validate: Run validation and raise on blocking errors.
        expected_interval_minutes: Bar interval for gap detection.

    Returns:
        Normalised OHLCV frame indexed by timestamp.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the data is malformed or fails validation.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"No such data file: {csv_path}")

    frame = normalise_frame(pd.read_csv(csv_path))
    logger.info("Loaded %d rows from %s", len(frame), csv_path.name)

    if validate:
        report = validate_ohlcv(frame, expected_interval_minutes)
        for warning in report.warnings:
            logger.warning("Data quality: %s", warning)
        if not report.is_usable:
            raise ValueError(
                f"{csv_path.name} failed validation:\n"
                + "\n".join(f"  - {e}" for e in report.errors)
            )

    return frame


def load_parquet(path: str | Path, validate: bool = True) -> pd.DataFrame:
    """Load OHLCV data from Parquet.

    Args:
        path: Path to the Parquet file.
        validate: Run validation and raise on blocking errors.

    Returns:
        Normalised OHLCV frame.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If validation fails.
    """
    parquet_path = Path(path)
    if not parquet_path.exists():
        raise FileNotFoundError(f"No such data file: {parquet_path}")

    frame = normalise_frame(pd.read_parquet(parquet_path))
    if validate:
        report = validate_ohlcv(frame)
        if not report.is_usable:
            raise ValueError(f"{parquet_path.name} failed validation: {'; '.join(report.errors)}")
    return frame


def load_any(path: str | Path, validate: bool = True) -> pd.DataFrame:
    """Load OHLCV data, dispatching on file extension.

    Args:
        path: Path to a ``.csv``, ``.parquet``, or ``.pq`` file.
        validate: Run validation.

    Returns:
        Normalised OHLCV frame.

    Raises:
        ValueError: For an unsupported extension.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return load_csv(path, validate)
    if suffix in {".parquet", ".pq"}:
        return load_parquet(path, validate)
    raise ValueError(f"Unsupported data format '{suffix}'. Use .csv or .parquet.")


def split_data(
    frame: pd.DataFrame,
    train: float = 0.6,
    validation: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split chronologically into train, validation, and out-of-sample sets.

    The split is chronological, never random. A random split leaks future
    information into training and is meaningless for time series.

    Args:
        frame: Full history.
        train: Training fraction.
        validation: Validation fraction. The remainder is out-of-sample.

    Returns:
        Tuple of ``(train, validation, out_of_sample)`` frames.

    Raises:
        ValueError: If the fractions are not a valid split.

    Example:
        >>> import pandas as pd
        >>> idx = pd.date_range("2026-01-01", periods=100, freq="5min")
        >>> df = pd.DataFrame(
        ...     {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
        ...     index=idx,
        ... )
        >>> a, b, c = split_data(df)
        >>> len(a), len(b), len(c)
        (60, 20, 20)
    """
    if not 0 < train < 1 or not 0 < validation < 1 or train + validation >= 1:
        raise ValueError(
            f"Invalid split: train={train}, validation={validation}. "
            "Both must be in (0, 1) and sum to less than 1."
        )

    total = len(frame)
    train_end = int(total * train)
    validation_end = train_end + int(total * validation)

    return (
        frame.iloc[:train_end],
        frame.iloc[train_end:validation_end],
        frame.iloc[validation_end:],
    )


def resample_ohlcv(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate bars to a longer interval.

    Args:
        frame: Normalised OHLCV frame.
        minutes: Target interval in minutes. Must exceed the source interval.

    Returns:
        Resampled frame with empty periods removed, so no synthetic bars are
        created for closed-market intervals.
    """
    rules: dict[str, Any] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in frame.columns:
        rules["volume"] = "sum"
    if "open_interest" in frame.columns:
        rules["open_interest"] = "last"

    resampled = frame.resample(f"{minutes}min", label="left", closed="left").agg(rules)
    return resampled.dropna(subset=["open", "high", "low", "close"])


def describe_coverage(frame: pd.DataFrame) -> dict[str, Any]:
    """Summarise data coverage for reporting.

    Args:
        frame: Normalised OHLCV frame.

    Returns:
        Mapping describing span, density, and price range.
    """
    if frame.empty:
        return {"rows": 0, "usable": False}

    days = sorted({ts.date() for ts in frame.index})
    per_day = frame.groupby(frame.index.date).size()

    return {
        "rows": len(frame),
        "start": frame.index[0].isoformat(),
        "end": frame.index[-1].isoformat(),
        "trading_days": len(days),
        "calendar_days": (days[-1] - days[0]).days + 1,
        "bars_per_day_mean": round(float(per_day.mean()), 1),
        "bars_per_day_min": int(per_day.min()),
        "bars_per_day_max": int(per_day.max()),
        "short_days": int((per_day < per_day.median() * 0.8).sum()),
        "close_range": [
            round(float(frame["close"].min()), 2),
            round(float(frame["close"].max()), 2),
        ],
        "has_volume": "volume" in frame.columns,
    }


def synthetic_ohlcv(
    bars: int = 2000,
    start_price: float = 24000.0,
    volatility: float = 0.0008,
    drift: float = 0.0,
    interval_minutes: int = 5,
    seed: int = 42,
    start: str = "2026-01-01 09:15",
) -> pd.DataFrame:
    """Generate a synthetic OHLCV series for testing the engine.

    This exists to test the **harness**, not to evaluate strategies. A result
    produced on synthetic data says nothing about a strategy's real edge, because
    geometric Brownian motion has none of the autocorrelation, volatility
    clustering, or regime structure of a real index.

    Bars are placed only inside 09:15-15:30 on weekdays, so session-aware code
    paths behave as they would on real data.

    Args:
        bars: Number of bars to generate.
        start_price: Starting price.
        volatility: Per-bar return standard deviation.
        drift: Per-bar mean return.
        interval_minutes: Bar interval.
        seed: RNG seed for reproducibility.
        start: First bar timestamp.

    Returns:
        Normalised OHLCV frame.
    """
    rng = np.random.default_rng(seed)

    timestamps: list[pd.Timestamp] = []
    cursor = pd.Timestamp(start)
    while len(timestamps) < bars:
        if cursor.weekday() < 5 and NSE_SESSION_START <= cursor.time() <= NSE_SESSION_END:
            timestamps.append(cursor)
        cursor += pd.Timedelta(minutes=interval_minutes)
        if cursor.time() > NSE_SESSION_END:
            cursor = (cursor + pd.Timedelta(days=1)).replace(hour=9, minute=15)

    returns = rng.normal(drift, volatility, bars)
    closes = start_price * np.exp(np.cumsum(returns))
    opens = np.concatenate([[start_price], closes[:-1]])

    # Wick sizes drawn independently so high and low bracket open and close.
    upper = np.abs(rng.normal(0, volatility * 0.6, bars))
    lower = np.abs(rng.normal(0, volatility * 0.6, bars))
    highs = np.maximum(opens, closes) * (1 + upper)
    lows = np.minimum(opens, closes) * (1 - lower)
    volumes = rng.integers(20_000, 200_000, bars)

    return pd.DataFrame(
        {
            "open": np.round(opens, 2),
            "high": np.round(highs, 2),
            "low": np.round(lows, 2),
            "close": np.round(closes, 2),
            "volume": volumes,
        },
        index=pd.DatetimeIndex(timestamps, name="timestamp"),
    )
