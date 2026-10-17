"""Normalization layer for raw Upstox historical-candle API responses.

Upstox candle responses (both V3 regular and V2 expired-instrument endpoints)
share the same envelope::

    {"status": "success", "data": {"candles": [
        ["2026-01-02T09:15:00+05:30", 21150.5, 21160.0, 21140.0, 21155.25, 12500, 0],
        ...
    ]}}

Each candle row is ``[timestamp_iso8601, open, high, low, close, volume,
open_interest]``. This module converts that shape into :class:`NormalizedBar`
rows matching the columns the downstream store (owned by a parallel agent)
expects to persist.

Timezone handling (spec section 20) is the sharp edge here. Upstox emits
ISO 8601 timestamps *with* an explicit offset (IST, ``+05:30``) — never
naive. Every ``datetime`` object this module constructs is timezone-aware;
none of the functions below ever produce or accept a naive datetime. The
canonical ``timestamp`` field on :class:`NormalizedBar` is UTC, but the raw
vendor string is always preserved verbatim in ``source_timestamp`` so no
information is lost in the conversion.

"Absence, not a default" (the convention already used throughout
``research/data``): a candle that vendor genuinely reports as
``volume=0``/``open_interest=0`` is a real observation and is kept as
``0.0``. A candle that is simply not present in the ``candles`` array
produces no :class:`NormalizedBar` at all — it is never synthesized as a row
with ``None`` prices. There is therefore no "missing bar" sentinel row;
gap detection over an actually-received bar list is handled downstream by
``validation.py`` and ``coverage.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: Expected length of a raw Upstox candle array:
#: [timestamp, open, high, low, close, volume, open_interest]
_CANDLE_ROW_LENGTH = 7


@dataclass(frozen=True)
class NormalizedBar:
    """One normalized OHLCV bar, ready for the downstream store's schema.

    See module docstring for the "absence, not a default" rule governing
    ``volume``/``open_interest``.
    """

    provider: str
    instrument_key: str
    underlying: str
    expiry: str | None
    strike: float | None
    option_type: str | None  # "CE" | "PE" | None
    exchange: str
    segment: str
    interval_unit: str
    interval_value: int
    timestamp: str  # UTC ISO8601, canonical
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    open_interest: float | None
    source_timestamp: str  # raw vendor timestamp string, exactly as received
    ingested_at: str
    dataset_version: str | None
    data_source_kind: str = "VENDOR_NATIVE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "instrument_key": self.instrument_key,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "exchange": self.exchange,
            "segment": self.segment,
            "interval_unit": self.interval_unit,
            "interval_value": self.interval_value,
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "source_timestamp": self.source_timestamp,
            "ingested_at": self.ingested_at,
            "dataset_version": self.dataset_version,
            "data_source_kind": self.data_source_kind,
        }


def _parse_vendor_timestamp(raw: str) -> datetime:
    """Parse a vendor ISO8601-with-offset timestamp into a timezone-aware datetime.

    Raises ``ValueError`` (caught by the caller, turned into a per-row
    warning) if the string has no offset or is otherwise unparseable —
    never silently treats it as naive/UTC.
    """
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {raw!r} has no timezone offset; refusing to assume one")
    return dt


def _to_canonical_utc(dt: datetime) -> str:
    """Convert an aware datetime to canonical UTC ISO8601 string."""
    return dt.astimezone(timezone.utc).isoformat()


def normalize_candle_response(
    raw_response: dict,
    *,
    provider: str,
    instrument_key: str,
    underlying: str,
    expiry: str | None,
    strike: float | None,
    option_type: str | None,
    exchange: str,
    segment: str,
    interval_unit: str,
    interval_value: int,
    dataset_version: str | None = None,
) -> tuple[list[NormalizedBar], list[str]]:
    """Normalize one Upstox candle response into a list of :class:`NormalizedBar`.

    Never raises on a malformed *individual* candle row — such a row is
    skipped and a warning describing exactly what was wrong is appended, so
    one bad row does not sink an entire response. Raises ``ValueError`` only
    when the top-level envelope itself does not match the documented
    ``{"status": ..., "data": {"candles": [...]}}`` shape at all — that is
    MALFORMED_RESPONSE territory, not a per-row skip.

    Returns:
        ``(bars, warnings)``.
    """
    if not isinstance(raw_response, dict):
        raise ValueError(
            f"MALFORMED_RESPONSE: expected a dict envelope, got {type(raw_response).__name__}"
        )
    if "data" not in raw_response:
        raise ValueError(
            "MALFORMED_RESPONSE: response has no 'data' key; not the documented "
            "{'status':..., 'data':{'candles':[...]}} shape"
        )
    data = raw_response["data"]
    if not isinstance(data, dict) or "candles" not in data:
        raise ValueError(
            "MALFORMED_RESPONSE: response['data'] has no 'candles' key; not the "
            "documented {'status':..., 'data':{'candles':[...]}} shape"
        )
    candles = data["candles"]
    if not isinstance(candles, list):
        raise ValueError(
            f"MALFORMED_RESPONSE: response['data']['candles'] is not a list "
            f"(got {type(candles).__name__})"
        )

    bars: list[NormalizedBar] = []
    warnings: list[str] = []
    ingested_at = datetime.now(timezone.utc).isoformat()

    for idx, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) != _CANDLE_ROW_LENGTH:
            actual_len = len(row) if isinstance(row, (list, tuple)) else "n/a"
            warnings.append(
                f"row {idx}: expected a {_CANDLE_ROW_LENGTH}-element candle array, "
                f"got length {actual_len} ({row!r}); row skipped"
            )
            continue

        raw_ts, raw_open, raw_high, raw_low, raw_close, raw_volume, raw_oi = row

        try:
            dt = _parse_vendor_timestamp(raw_ts)
        except (ValueError, TypeError) as exc:
            warnings.append(f"row {idx}: unparseable timestamp {raw_ts!r} ({exc}); row skipped")
            continue

        try:
            o = float(raw_open)
            h = float(raw_high)
            low_ = float(raw_low)
            c = float(raw_close)
        except (TypeError, ValueError):
            warnings.append(
                f"row {idx}: non-numeric OHLC value(s) "
                f"(open={raw_open!r}, high={raw_high!r}, low={raw_low!r}, close={raw_close!r}); "
                "row skipped"
            )
            continue

        # Volume/OI: vendor may legitimately send 0 (a real zero, kept as
        # 0.0) or None/missing-from-row is not possible here since the row
        # was well-formed length-7; but guard non-numeric junk defensively.
        try:
            volume = None if raw_volume is None else float(raw_volume)
        except (TypeError, ValueError):
            warnings.append(f"row {idx}: non-numeric volume {raw_volume!r}; row skipped")
            continue
        try:
            open_interest = None if raw_oi is None else float(raw_oi)
        except (TypeError, ValueError):
            warnings.append(f"row {idx}: non-numeric open_interest {raw_oi!r}; row skipped")
            continue

        bars.append(
            NormalizedBar(
                provider=provider,
                instrument_key=instrument_key,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                exchange=exchange,
                segment=segment,
                interval_unit=interval_unit,
                interval_value=interval_value,
                timestamp=_to_canonical_utc(dt),
                open=o,
                high=h,
                low=low_,
                close=c,
                volume=volume,
                open_interest=open_interest,
                source_timestamp=raw_ts,
                ingested_at=ingested_at,
                dataset_version=dataset_version,
            )
        )

    return bars, warnings


#: IST is a fixed +05:30 offset with no DST, per Upstox's documented
#: timestamp format. Kept as a module constant so the session-boundary
#: helper below and tests share one definition.
IST = timezone(timedelta(hours=5, minutes=30))


def is_within_ist_market_session(
    utc_timestamp: str, *, session_start: str = "09:15", session_end: str = "15:30"
) -> bool:
    """Check whether a canonical UTC timestamp falls within an IST market session.

    Converts the UTC timestamp *back* to IST wall-clock time for the
    comparison — never assumes a fixed hour offset shortcut, since that
    breaks the moment someone applies a naive UTC-hour comparison. Bounds
    are inclusive of ``session_start`` and exclusive of ``session_end``,
    matching the documented 09:15-15:30 IST session with 15:30 being the
    close, not a tradeable bar-open timestamp.

    Args:
        utc_timestamp: A canonical UTC ISO8601 string (as produced by
            :func:`normalize_candle_response`).
        session_start: ``HH:MM`` IST lower bound (inclusive).
        session_end: ``HH:MM`` IST upper bound (exclusive).
    """
    dt_utc = datetime.fromisoformat(utc_timestamp)
    if dt_utc.tzinfo is None:
        raise ValueError(f"{utc_timestamp!r} is naive; expected an aware UTC timestamp")
    dt_ist = dt_utc.astimezone(IST)

    start_h, start_m = (int(x) for x in session_start.split(":"))
    end_h, end_m = (int(x) for x in session_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    bar_minutes = dt_ist.hour * 60 + dt_ist.minute

    return start_minutes <= bar_minutes < end_minutes
