"""NSE/BSE session clock in Asia/Kolkata.

The cache polls at two different rates depending on whether a session is live,
and the charter forbids acting on stale data, so "is the market open" has to be
a precise question rather than a guess.

Timezone handling is explicit: the server may run in any local timezone, or in
UTC inside a container, so every comparison is made against an
``Asia/Kolkata``-aware timestamp rather than naive local time.

Holiday caveat: NSE publishes trading holidays annually and they shift each
year. The list below covers the current year and is necessarily incomplete for
future years, so :func:`is_trading_holiday` returning False is not proof that a
session is running. Special sessions (Muhurat trading, budget-day sessions) are
also not modelled. The authoritative signal that data is flowing is a
successful quote with a recent exchange timestamp, which the cache tracks
separately.

Example:
    >>> from core.market_hours import get_market_status
    >>> status = get_market_status()
    >>> set(status) >= {"is_open", "session", "now_ist"}
    True
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Equity and equity-derivatives continuous session.
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Pre-open call auction: order collection, randomised match, then transition.
PRE_OPEN_START = time(9, 0)
PRE_OPEN_END = time(9, 15)

# Post-close session for equity cash.
POST_CLOSE_START = time(15, 40)
POST_CLOSE_END = time(16, 0)

# Currency derivatives run later than equities.
CDS_OPEN = time(9, 0)
CDS_CLOSE = time(17, 0)

# MCX evening session (varies seasonally; the later close applies in summer).
MCX_OPEN = time(9, 0)
MCX_CLOSE = time(23, 30)

SessionName = Literal[
    "CLOSED_WEEKEND",
    "CLOSED_HOLIDAY",
    "PRE_MARKET",
    "PRE_OPEN",
    "OPEN",
    "POST_CLOSE",
    "AFTER_HOURS",
]

# NSE trading holidays for 2026, from the NSE holiday circular.
#
# Verify against https://www.nseindia.com/resources/exchange-communication-holidays
# at the start of each calendar year. An out-of-date list makes the poller
# assume a session is running on a holiday, which wastes API quota but cannot
# produce wrong prices, because quotes simply do not update.
NSE_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 26),  # Republic Day
        date(2026, 3, 4),  # Holi
        date(2026, 3, 21),  # Id-Ul-Fitr (Ramzan Id)
        date(2026, 3, 27),  # Ram Navami
        date(2026, 3, 31),  # Mahavir Jayanti
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 14),  # Dr. Ambedkar Jayanti
        date(2026, 5, 1),  # Maharashtra Day
        date(2026, 5, 27),  # Bakri Id
        date(2026, 6, 26),  # Muharram
        date(2026, 8, 15),  # Independence Day
        date(2026, 8, 26),  # Ganesh Chaturthi
        date(2026, 10, 2),  # Gandhi Jayanti / Dussehra
        date(2026, 10, 20),  # Diwali Laxmi Pujan
        date(2026, 11, 5),  # Guru Nanak Jayanti
        date(2026, 12, 25),  # Christmas
    }
)

NSE_HOLIDAYS_2025: frozenset[date] = frozenset(
    {
        date(2025, 2, 26),
        date(2025, 3, 14),
        date(2025, 3, 31),
        date(2025, 4, 10),
        date(2025, 4, 14),
        date(2025, 4, 18),
        date(2025, 5, 1),
        date(2025, 8, 15),
        date(2025, 8, 27),
        date(2025, 10, 2),
        date(2025, 10, 21),
        date(2025, 10, 22),
        date(2025, 11, 5),
        date(2025, 12, 25),
    }
)

_HOLIDAYS_BY_YEAR: dict[int, frozenset[date]] = {
    2025: NSE_HOLIDAYS_2025,
    2026: NSE_HOLIDAYS_2026,
}


@dataclass(frozen=True)
class MarketStatus:
    """Point-in-time session state.

    Attributes:
        now_ist: Current IST timestamp.
        session: Which session window is active.
        is_open: True only during the continuous session.
        is_trading_day: True when the date is a weekday and not a known holiday.
        holiday_data_available: False when the year's holiday list is absent,
            meaning ``is_trading_day`` is weekday-only and less reliable.
        seconds_to_open: Seconds until the next open, when closed.
        seconds_to_close: Seconds until close, when open.
        next_session_start: IST timestamp of the next open.
    """

    now_ist: datetime
    session: SessionName
    is_open: bool
    is_trading_day: bool
    holiday_data_available: bool
    seconds_to_open: float | None
    seconds_to_close: float | None
    next_session_start: datetime | None

    def as_dict(self) -> dict[str, Any]:
        """Return the status as a JSON-serialisable mapping."""
        return {
            "now_ist": self.now_ist.isoformat(),
            "session": self.session,
            "is_open": self.is_open,
            "is_trading_day": self.is_trading_day,
            "holiday_data_available": self.holiday_data_available,
            "seconds_to_open": (
                round(self.seconds_to_open, 1) if self.seconds_to_open is not None else None
            ),
            "seconds_to_close": (
                round(self.seconds_to_close, 1) if self.seconds_to_close is not None else None
            ),
            "next_session_start": (
                self.next_session_start.isoformat() if self.next_session_start else None
            ),
        }


def now_ist() -> datetime:
    """Return the current time in IST.

    Returns:
        Timezone-aware ``Asia/Kolkata`` timestamp.
    """
    return datetime.now(IST)


def to_ist(moment: datetime) -> datetime:
    """Convert any datetime to IST.

    A naive input is assumed to already be IST, which is the convention for
    timestamps parsed out of SmartAPI responses.

    Args:
        moment: Aware or naive datetime.

    Returns:
        Aware IST datetime.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def is_trading_holiday(day: date) -> bool:
    """Check a date against the known NSE holiday list.

    Args:
        day: Calendar date to test.

    Returns:
        True when the date is a listed holiday. False also results when the
        year is not covered, so pair this with
        :func:`holiday_data_available`.
    """
    return day in _HOLIDAYS_BY_YEAR.get(day.year, frozenset())


def holiday_data_available(day: date) -> bool:
    """Report whether a holiday list exists for the date's year.

    Args:
        day: Calendar date to test.

    Returns:
        True when the year is covered by :data:`_HOLIDAYS_BY_YEAR`.
    """
    return day.year in _HOLIDAYS_BY_YEAR


def is_trading_day(day: date | None = None) -> bool:
    """Check whether a date is a plausible trading day.

    Args:
        day: Date to test. Defaults to today in IST.

    Returns:
        True when the date is Monday to Friday and not a known holiday.
    """
    target = day or now_ist().date()
    if target.weekday() >= 5:
        return False
    return not is_trading_holiday(target)


def is_market_open(moment: datetime | None = None) -> bool:
    """Check whether the continuous equity session is running.

    Args:
        moment: Instant to test. Defaults to now.

    Returns:
        True when inside 09:15-15:30 IST on a trading day.
    """
    instant = to_ist(moment) if moment else now_ist()
    if not is_trading_day(instant.date()):
        return False
    return MARKET_OPEN <= instant.time() <= MARKET_CLOSE


def next_market_open(after: datetime | None = None) -> datetime:
    """Find the next session open at or after a given instant.

    Scans forward up to 30 days, which covers any realistic holiday cluster.

    Args:
        after: Starting instant. Defaults to now.

    Returns:
        IST timestamp of the next open.
    """
    instant = to_ist(after) if after else now_ist()

    if is_trading_day(instant.date()) and instant.time() < MARKET_OPEN:
        return instant.replace(
            hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0
        )

    candidate = instant.date() + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(candidate):
            return datetime.combine(candidate, MARKET_OPEN, tzinfo=IST)
        candidate += timedelta(days=1)

    logger.warning("No trading day found within 30 days of %s", instant.date())
    return datetime.combine(candidate, MARKET_OPEN, tzinfo=IST)


def classify_session(moment: datetime | None = None) -> SessionName:
    """Name the session window containing an instant.

    Args:
        moment: Instant to classify. Defaults to now.

    Returns:
        The session name.
    """
    instant = to_ist(moment) if moment else now_ist()
    clock = instant.time()

    if instant.date().weekday() >= 5:
        return "CLOSED_WEEKEND"
    if is_trading_holiday(instant.date()):
        return "CLOSED_HOLIDAY"
    if clock < PRE_OPEN_START:
        return "PRE_MARKET"
    if PRE_OPEN_START <= clock < PRE_OPEN_END:
        return "PRE_OPEN"
    if MARKET_OPEN <= clock <= MARKET_CLOSE:
        return "OPEN"
    if POST_CLOSE_START <= clock <= POST_CLOSE_END:
        return "POST_CLOSE"
    return "AFTER_HOURS"


def get_market_status(moment: datetime | None = None) -> dict[str, Any]:
    """Build a full session snapshot.

    Args:
        moment: Instant to describe. Defaults to now.

    Returns:
        Mapping produced by :meth:`MarketStatus.as_dict`.

    Example:
        >>> status = get_market_status()
        >>> status["session"] in {
        ...     "CLOSED_WEEKEND", "CLOSED_HOLIDAY", "PRE_MARKET",
        ...     "PRE_OPEN", "OPEN", "POST_CLOSE", "AFTER_HOURS",
        ... }
        True
    """
    instant = to_ist(moment) if moment else now_ist()
    session = classify_session(instant)
    market_open = session == "OPEN"

    seconds_to_close: float | None = None
    seconds_to_open: float | None = None
    next_open: datetime | None = None

    if market_open:
        close_at = instant.replace(
            hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0
        )
        seconds_to_close = max(0.0, (close_at - instant).total_seconds())
    else:
        next_open = next_market_open(instant)
        seconds_to_open = max(0.0, (next_open - instant).total_seconds())

    status = MarketStatus(
        now_ist=instant,
        session=session,
        is_open=market_open,
        is_trading_day=is_trading_day(instant.date()),
        holiday_data_available=holiday_data_available(instant.date()),
        seconds_to_open=seconds_to_open,
        seconds_to_close=seconds_to_close,
        next_session_start=next_open,
    )
    return status.as_dict()


def should_fetch_candles(moment: datetime | None = None) -> bool:
    """Decide whether candle history is worth fetching now.

    Candles only change while the session runs. Polling them after hours burns
    rate-limit budget for identical data, so the cache stops. A short grace
    period past the close captures the final bar once it is published.

    Args:
        moment: Instant to test. Defaults to now.

    Returns:
        True during the session and for 15 minutes after the close.
    """
    instant = to_ist(moment) if moment else now_ist()
    if not is_trading_day(instant.date()):
        return False
    grace_end = (
        datetime.combine(instant.date(), MARKET_CLOSE, tzinfo=IST) + timedelta(minutes=15)
    ).time()
    return MARKET_OPEN <= instant.time() <= grace_end


def poll_interval_seconds(active: int, idle: int, moment: datetime | None = None) -> int:
    """Pick the cache poll interval for the current session.

    Args:
        active: Interval to use while the market is open.
        idle: Interval to use otherwise.
        moment: Instant to evaluate. Defaults to now.

    Returns:
        ``active`` during the continuous session or pre-open, else ``idle``.

    Example:
        >>> poll_interval_seconds(5, 60) in (5, 60)
        True
    """
    session = classify_session(moment)
    return active if session in {"OPEN", "PRE_OPEN"} else idle


def session_bounds(day: date | None = None) -> tuple[datetime, datetime]:
    """Return the session open and close timestamps for a date.

    Useful for bounding historical candle requests to real session windows.

    Args:
        day: Date of interest. Defaults to today in IST.

    Returns:
        Tuple of aware IST ``(open, close)`` timestamps.
    """
    target = day or now_ist().date()
    return (
        datetime.combine(target, MARKET_OPEN, tzinfo=IST),
        datetime.combine(target, MARKET_CLOSE, tzinfo=IST),
    )


def previous_trading_day(day: date | None = None) -> date:
    """Find the most recent trading day strictly before a date.

    Args:
        day: Reference date. Defaults to today in IST.

    Returns:
        The previous trading day.
    """
    candidate = (day or now_ist().date()) - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return candidate
