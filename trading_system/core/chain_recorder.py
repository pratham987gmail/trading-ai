"""Option chain recording loop.

Separated from ``scripts/record_chains.py`` so the loop can be tested without a
broker: the recorder takes its fetcher, store, and clock as arguments rather than
constructing them. The script is a thin CLI over this.

Three behaviours here are less obvious than they look.

**A cycle that fails is recorded, not just logged.** Every pass writes a row to
``cycles`` whether or not it produced data, so a gap in coverage is queryable
afterwards. The alternative, noticing an absence in a log file weeks later, does
not work for data that cannot be re-fetched.

**Captures are aligned to interval boundaries from the session open.** Snapshots
land on 09:15, 09:20, 09:25 rather than drifting with process start time, so bars
built from different days line up and a restart mid-session does not create a
second, offset series.

**A stalled feed is distinguished from an unchanged market.** Our own capture
clock always advances, so a frozen broker feed would otherwise produce a long run
of identical snapshots that look like genuine observations. The recorder compares
the broker's own ``exchange_timestamp`` against the previous snapshot and flags
the chain when nothing moved at all.

Example:
    >>> recorder = ChainRecorder(fetcher, store, RecorderConfig(["NIFTY"]))
    >>> result = recorder.record_cycle()      # doctest: +SKIP
    >>> result.snapshots_written              # doctest: +SKIP
    3
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from core.market_hours import (
    classify_session,
    get_market_status,
    holiday_data_available,
    is_market_open,
    is_trading_day,
    now_ist,
    session_bounds,
)
from core.option_store import OptionChainStore

logger = logging.getLogger(__name__)

#: Below this fraction of expected legs a snapshot is flagged. A dropped
#: fifty-token quote chunk on a 41-strike chain costs ~60% of the legs, so the
#: threshold has to sit well above that to be useful.
MIN_COMPLETENESS = 0.90

#: Longest a closed-market sleep may run before the loop re-checks. Keeps Ctrl-C
#: responsive and stops an unlisted holiday from causing a blind 24-hour wait.
MAX_IDLE_SLEEP_SECONDS = 300.0


class ChainFetcher(Protocol):
    """The subset of :class:`~core.data_fetcher.DataFetcher` the recorder needs.

    Declared as a protocol so tests can substitute a stub without a broker
    session, and so the recorder cannot quietly grow a dependency on the rest of
    the fetcher.
    """

    def fetch_expiries(self, underlying: str, exchange: str = "NFO") -> list[str]:
        """Return unexpired expiries, soonest first."""
        ...

    def fetch_option_chain(
        self,
        underlying: str,
        expiry: str | None = None,
        strike_count: int = 20,
        exchange: str = "NFO",
    ) -> dict[str, Any] | None:
        """Return an assembled option chain, or ``None`` on failure."""
        ...


@dataclass
class RecorderConfig:
    """Recording configuration.

    Attributes:
        underlyings: Underlyings to record, e.g. ``["NIFTY", "BANKNIFTY"]``.
        expiries: Number of nearest expiries per underlying.
        strike_count: Strikes each side of spot. The chain carries up to
            ``2 * strike_count + 1`` strikes.
        interval_seconds: Target seconds between capture cycles.
        exchange: Derivatives segment.
        stall_alert_seconds: Log at CRITICAL when nothing has been captured for
            this long while the market is open.
        min_completeness: Flag a snapshot below this leg-completeness ratio.
        align_to_session: Align captures to interval boundaries from the session
            open rather than to process start time.
        ignore_market_hours: Record regardless of the session clock. For testing
            only; the data is real but the market is not moving.
    """

    underlyings: list[str]
    expiries: int = 3
    strike_count: int = 20
    interval_seconds: int = 300
    exchange: str = "NFO"
    stall_alert_seconds: int = 1800
    min_completeness: float = MIN_COMPLETENESS
    align_to_session: bool = True
    ignore_market_hours: bool = False

    def __post_init__(self) -> None:
        self.underlyings = [u.strip().upper() for u in self.underlyings if u and u.strip()]
        if not self.underlyings:
            raise ValueError("At least one underlying is required")
        if self.expiries < 1:
            raise ValueError("expiries must be at least 1")
        if self.strike_count < 1:
            raise ValueError("strike_count must be at least 1")
        if self.interval_seconds < 30:
            # The quote bucket runs at 1 request/second and one cycle of three
            # underlyings costs roughly twenty quote calls, so anything under
            # thirty seconds cannot complete before the next cycle is due.
            raise ValueError(
                f"interval_seconds={self.interval_seconds} is below the 30s floor. "
                "The quote rate limiter cannot complete a cycle faster than that."
            )

    @property
    def chains_per_cycle(self) -> int:
        """Number of chains fetched per cycle."""
        return len(self.underlyings) * self.expiries

    def estimated_broker_calls(self) -> int:
        """Estimate broker calls per cycle, for a rate-limit sanity check.

        Each chain costs one LTP call for spot plus one quote call per fifty
        tokens, and a strike carries two tokens.

        Returns:
            Estimated call count per cycle.
        """
        tokens = (2 * self.strike_count + 1) * 2
        quote_calls = -(-tokens // 50)
        return self.chains_per_cycle * (1 + quote_calls)

    def as_dict(self) -> dict[str, Any]:
        """Return the configuration for logging and the cycle notes."""
        return {
            "underlyings": list(self.underlyings),
            "expiries": self.expiries,
            "strike_count": self.strike_count,
            "interval_seconds": self.interval_seconds,
            "exchange": self.exchange,
            "chains_per_cycle": self.chains_per_cycle,
            "estimated_broker_calls_per_cycle": self.estimated_broker_calls(),
        }


@dataclass
class CycleResult:
    """Outcome of one capture pass.

    Attributes:
        cycle_id: Identifier shared by every snapshot in the pass.
        started_at: Pass start.
        finished_at: Pass end.
        snapshots_written: Chains stored.
        snapshots_failed: Chains that could not be fetched or stored.
        snapshots_duplicate: Chains whose timestamp was already stored.
        legs_written: Option legs stored.
        issues: Validation findings across the pass.
    """

    cycle_id: str
    started_at: datetime
    finished_at: datetime | None = None
    snapshots_written: int = 0
    snapshots_failed: int = 0
    snapshots_duplicate: int = 0
    legs_written: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        """Wall-clock duration of the pass."""
        if self.finished_at is None:
            return 0.0
        return round((self.finished_at - self.started_at).total_seconds(), 2)

    @property
    def succeeded(self) -> bool:
        """True when at least one chain was stored."""
        return self.snapshots_written > 0

    def summary(self) -> str:
        """Return a one-line summary for the log."""
        return (
            f"{self.snapshots_written} written, {self.snapshots_failed} failed, "
            f"{self.snapshots_duplicate} duplicate, {self.legs_written} legs, "
            f"{self.duration_seconds}s"
        )


def validate_chain(chain: dict[str, Any], min_completeness: float = MIN_COMPLETENESS) -> list[str]:
    """Check a chain for problems worth recording alongside the data.

    Note what is *not* validated: implied volatility. The quote endpoint does not
    supply it, and this system does not estimate market data, so there is no IV
    to bound-check. IV is recoverable later from the recorded premium, spot,
    strike, and timestamp; what cannot be recovered is a quote that was never
    stored, which is what these checks protect.

    Args:
        chain: A ``fetch_option_chain`` result.
        min_completeness: Flag below this leg-completeness ratio.

    Returns:
        Human-readable findings. Empty when the chain looks sound.
    """
    issues: list[str] = []

    spot = chain.get("spot")
    if spot is None:
        issues.append("spot missing")
    elif not isinstance(spot, bool) and float(spot) <= 0:
        issues.append(f"spot is not positive ({spot})")

    strikes = chain.get("strikes") or []
    if not strikes:
        issues.append("no strikes returned")
        return issues

    seen: set[float] = set()
    legs_expected = len(strikes) * 2
    legs_present = 0
    crossed = 0
    ltp_outside_book = 0
    negative = 0

    for row in strikes:
        strike = row.get("strike")
        if strike is None:
            issues.append("a strike row has no strike price")
            continue
        if strike in seen:
            issues.append(f"duplicate strike {strike}")
        seen.add(float(strike))

        for key in ("call", "put"):
            leg = row.get(key)
            if not isinstance(leg, dict):
                continue
            legs_present += 1

            bid, ask, ltp = leg.get("bid"), leg.get("ask"), leg.get("ltp")
            if bid is not None and ask is not None:
                if bid > ask:
                    crossed += 1
                elif ltp is not None and not (bid <= ltp <= ask):
                    # Normal for a stale last trade on an illiquid strike, so it
                    # is counted rather than treated as an error.
                    ltp_outside_book += 1

            for name in ("volume", "open_interest", "bid_qty", "ask_qty"):
                value = leg.get(name)
                if value is not None and value < 0:
                    negative += 1

    if legs_expected:
        completeness = legs_present / legs_expected
        if completeness < min_completeness:
            issues.append(
                f"only {legs_present} of {legs_expected} legs returned "
                f"({completeness:.0%}); a dropped quote batch looks exactly like this"
            )

    if crossed:
        issues.append(f"{crossed} leg(s) have bid above ask")
    if ltp_outside_book:
        issues.append(f"{ltp_outside_book} leg(s) have LTP outside the bid-ask range")
    if negative:
        issues.append(f"{negative} negative size/OI value(s)")

    ladder = sorted(seen)
    if len(ladder) > 2:
        gaps = {round(b - a, 2) for a, b in zip(ladder[:-1], ladder[1:], strict=True)}
        if len(gaps) > 1:
            issues.append(f"irregular strike spacing: {sorted(gaps)}")

    return issues


def chain_fingerprint(chain: dict[str, Any]) -> tuple:
    """Build a fingerprint of a chain's broker-side state.

    Used to tell a frozen feed from an unchanged market. Built from the exchange
    timestamps and last traded prices, never from our own capture time, which
    always advances and so would make every snapshot look fresh.

    Args:
        chain: A ``fetch_option_chain`` result.

    Returns:
        A hashable fingerprint.
    """
    parts: list[Any] = [chain.get("spot")]
    for row in chain.get("strikes") or []:
        for key in ("call", "put"):
            leg = row.get(key)
            if isinstance(leg, dict):
                parts.append((leg.get("exchange_timestamp"), leg.get("ltp")))
    return tuple(parts)


class ChainRecorder:
    """Captures option chains on a schedule and stores them.

    Args:
        fetcher: Anything satisfying :class:`ChainFetcher`.
        store: Where snapshots are written.
        config: Recording configuration.
        clock: Returns the current IST time. Injectable so session gating can be
            tested off-hours.
        on_new_trading_day: Called once when the recorder first sees a new
            trading date. Use it to reload the instrument master: F&O tokens
            change on rollover, and a recorder running for weeks would otherwise
            keep quoting dead contracts.
        sleeper: Waits the given number of seconds. Defaults to an interruptible
            wait on the stop event. Injectable so the scheduling logic can be
            tested without waiting out a real interval.
    """

    def __init__(
        self,
        fetcher: ChainFetcher,
        store: OptionChainStore,
        config: RecorderConfig,
        clock: Callable[[], datetime] = now_ist,
        on_new_trading_day: Callable[[date], None] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """Initialise the recorder."""
        self.fetcher = fetcher
        self.store = store
        self.config = config
        self.clock = clock
        self.on_new_trading_day = on_new_trading_day
        self._sleeper = sleeper

        self._stop = threading.Event()
        self._started_at = clock()
        self._last_success_at: datetime | None = None
        self._stall_reported = False
        self._current_trading_day: date | None = None
        self._fingerprints: dict[tuple[str, str], tuple] = {}

        self.cycles_run = 0
        self.cycles_failed = 0
        self.snapshots_written = 0
        self.legs_written = 0

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    def record_cycle(self) -> CycleResult:
        """Capture every configured chain once.

        Never raises. A broker failure, a malformed chain, or a storage error is
        counted and logged; the pass continues to the next chain, because a
        single bad expiry must not cost the whole cycle.

        Returns:
            The :class:`CycleResult` for this pass.
        """
        started = self.clock()
        cycle_id = f"{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        result = CycleResult(cycle_id=cycle_id, started_at=started)

        try:
            self.store.start_cycle(cycle_id, started)
        except Exception as exc:  # noqa: BLE001 - storage must not abort a capture
            logger.error("Could not open cycle %s: %s", cycle_id, exc)

        for underlying in self.config.underlyings:
            expiries = self._resolve_expiries(underlying)
            if not expiries:
                result.snapshots_failed += self.config.expiries
                result.issues.append(f"{underlying}: no expiries available")
                continue

            for expiry in expiries:
                try:
                    self._capture(underlying, expiry, cycle_id, result)
                except Exception as exc:  # noqa: BLE001 - the loop must not die
                    # record_cycle promises never to raise. Anything unexpected
                    # here would otherwise propagate out of run() and end weeks
                    # of unattended recording.
                    logger.exception(
                        "Unexpected failure capturing %s %s: %s", underlying, expiry, exc
                    )
                    result.snapshots_failed += 1
                    result.issues.append(f"{underlying} {expiry}: unexpected {type(exc).__name__}")

        result.finished_at = self.clock()
        self.cycles_run += 1
        if not result.succeeded:
            self.cycles_failed += 1
        self.snapshots_written += result.snapshots_written
        self.legs_written += result.legs_written

        if result.succeeded:
            self._last_success_at = result.finished_at
            self._stall_reported = False

        try:
            self.store.finish_cycle(
                cycle_id,
                written=result.snapshots_written,
                failed=result.snapshots_failed,
                legs=result.legs_written,
                notes="; ".join(result.issues[:10]) or None,
                finished_at=result.finished_at,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not close cycle %s: %s", cycle_id, exc)

        self._warn_on_overrun(result)
        return result

    def _resolve_expiries(self, underlying: str) -> list[str]:
        """Return the nearest configured number of expiries."""
        try:
            available = self.fetcher.fetch_expiries(underlying, self.config.exchange)
        except Exception as exc:  # noqa: BLE001 - never abort a cycle
            logger.error("Expiry lookup failed for %s: %s", underlying, exc)
            return []
        return list(available[: self.config.expiries])

    def _capture(self, underlying: str, expiry: str, cycle_id: str, result: CycleResult) -> None:
        """Fetch, validate, and store one chain."""
        try:
            chain = self.fetcher.fetch_option_chain(
                underlying,
                expiry=expiry,
                strike_count=self.config.strike_count,
                exchange=self.config.exchange,
            )
        except Exception as exc:  # noqa: BLE001 - a broker fault is expected, not fatal
            logger.error("Chain fetch raised for %s %s: %s", underlying, expiry, exc)
            result.snapshots_failed += 1
            result.issues.append(f"{underlying} {expiry}: fetch raised {type(exc).__name__}")
            return

        if not chain:
            # fetch_option_chain returns None rather than raising, so this is the
            # normal shape of a broker outage or an unloaded instrument master.
            logger.warning("No chain returned for %s %s", underlying, expiry)
            result.snapshots_failed += 1
            result.issues.append(f"{underlying} {expiry}: no chain returned")
            return

        # Validation is a commentary on the data, never a gate on storing it. A
        # bug in a check must not cost a capture, because the quote is gone the
        # moment the cycle ends whereas a missing annotation can be recomputed.
        try:
            issues = validate_chain(chain, self.config.min_completeness)
        except Exception as exc:  # noqa: BLE001
            logger.error("Validation raised for %s %s: %s", underlying, expiry, exc)
            issues = [f"validation failed: {type(exc).__name__}"]

        try:
            fingerprint = chain_fingerprint(chain)
            key = (underlying, expiry)
            if self._fingerprints.get(key) == fingerprint:
                issues.append(
                    "broker state identical to the previous snapshot: exchange "
                    "timestamps and every LTP unchanged, so the feed may be frozen"
                )
            self._fingerprints[key] = fingerprint
        except Exception as exc:  # noqa: BLE001
            logger.error("Fingerprinting raised for %s %s: %s", underlying, expiry, exc)

        try:
            snapshot_id = self.store.write_snapshot(chain, cycle_id=cycle_id, issues=issues)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not store %s %s: %s", underlying, expiry, exc)
            result.snapshots_failed += 1
            result.issues.append(f"{underlying} {expiry}: store failed")
            return

        if snapshot_id is None:
            result.snapshots_duplicate += 1
            return

        legs = sum(
            1
            for row in chain.get("strikes") or []
            for key_name in ("call", "put")
            if isinstance(row.get(key_name), dict)
        )
        result.snapshots_written += 1
        result.legs_written += legs
        if issues:
            result.issues.append(f"{underlying} {expiry}: {issues[0]}")

        logger.info(
            "%s %s | %d strikes | %d legs | spot %s%s",
            underlying,
            expiry,
            len(chain.get("strikes") or []),
            legs,
            chain.get("spot"),
            f" | {len(issues)} issue(s)" if issues else "",
        )

    def _warn_on_overrun(self, result: CycleResult) -> None:
        """Warn when a cycle takes long enough to threaten the schedule."""
        budget = self.config.interval_seconds
        if result.duration_seconds > budget:
            logger.warning(
                "Cycle %s took %.1fs, longer than the %ds interval. Captures will "
                "be skipped. Reduce --expiries, --strike-count, or --symbols, or "
                "raise --interval.",
                result.cycle_id,
                result.duration_seconds,
                budget,
            )
        elif result.duration_seconds > budget * 0.8:
            logger.info(
                "Cycle %s used %.0f%% of the interval; little headroom left",
                result.cycle_id,
                result.duration_seconds / budget * 100,
            )

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def run(self, max_cycles: int | None = None) -> int:
        """Record until stopped, the session ends, or a cycle cap is reached.

        Args:
            max_cycles: Stop after this many cycles. ``None`` runs until
                :meth:`stop` is called.

        Returns:
            Number of cycles that stored at least one snapshot.
        """
        logger.info("Recorder starting: %s", self.config.as_dict())
        if self.config.ignore_market_hours:
            logger.warning(
                "Market-hours gating disabled. Quotes captured now are real but "
                "static; do not treat them as a trading session."
            )

        succeeded = 0
        while not self._stop.is_set():
            if max_cycles is not None and self.cycles_run >= max_cycles:
                break

            moment = self.clock()

            if not self.config.ignore_market_hours and not self._session_open(moment):
                if max_cycles is not None:
                    # A cycle budget means "do this much work now". Waiting hours
                    # for an open market would look like a hang, which is how
                    # --once on a weekend behaved before this guard.
                    logger.warning(
                        "Market is closed (%s) and a cycle limit was given, so "
                        "there is nothing to capture. Pass ignore_market_hours "
                        "to record a static feed anyway.",
                        classify_session(moment),
                    )
                    break
                self._sleep(self._idle_sleep_seconds(moment))
                continue

            self._roll_trading_day(moment.date())

            result = self.record_cycle()
            if result.succeeded:
                succeeded += 1
            else:
                logger.warning("Cycle %s stored nothing: %s", result.cycle_id, result.summary())

            self._check_stall(self.clock())

            if max_cycles is not None and self.cycles_run >= max_cycles:
                break
            self._sleep(self._seconds_until_next_capture(self.clock()))

        logger.info(
            "Recorder stopped after %d cycle(s): %d snapshots, %d legs, %d failed cycle(s)",
            self.cycles_run,
            self.snapshots_written,
            self.legs_written,
            self.cycles_failed,
        )
        return succeeded

    def stop(self) -> None:
        """Ask the loop to finish after the current cycle."""
        self._stop.set()

    def _sleep(self, seconds: float) -> None:
        """Sleep interruptibly, so a stop request is honoured mid-wait."""
        if seconds <= 0:
            return
        if self._sleeper is not None:
            self._sleeper(seconds)
            return
        self._stop.wait(seconds)

    @staticmethod
    def _session_open(moment: datetime) -> bool:
        """True when the exchange is open, with a holiday-coverage warning."""
        if not holiday_data_available(moment.date()) and is_trading_day(moment.date()):
            logger.warning(
                "No holiday list for %d. If today is a holiday the recorder will "
                "capture a static feed and flag it as frozen rather than skip it.",
                moment.year,
            )
        return is_market_open(moment)

    def _idle_sleep_seconds(self, moment: datetime) -> float:
        """Seconds to wait while the market is closed."""
        status = get_market_status(moment)
        wait = status.get("seconds_to_open")
        if wait is None or wait <= 0:
            return MAX_IDLE_SLEEP_SECONDS
        # Capped so the loop re-checks periodically rather than trusting a
        # possibly incomplete holiday list for a full day.
        return min(float(wait), MAX_IDLE_SLEEP_SECONDS)

    def _seconds_until_next_capture(self, moment: datetime) -> float:
        """Seconds until the next aligned capture boundary.

        Alignment is measured from the session open, so snapshots land on the
        same clock positions every day and a mid-session restart rejoins the
        existing series instead of starting an offset one.

        Args:
            moment: Current time.

        Returns:
            Seconds to wait. Zero when a boundary has already passed.
        """
        interval = float(self.config.interval_seconds)
        if not self.config.align_to_session:
            return interval

        try:
            open_at, close_at = session_bounds(moment.date())
        except Exception:  # noqa: BLE001 - fall back to a plain interval
            return interval

        if moment >= close_at:
            return interval

        elapsed = (moment - open_at).total_seconds()
        if elapsed < 0:
            return min(-elapsed, MAX_IDLE_SLEEP_SECONDS)

        steps = int(elapsed // interval) + 1
        target = open_at + timedelta(seconds=steps * interval)
        # A cycle that overran its slot lands here; skip to the next boundary
        # rather than firing immediately and compounding the overrun.
        while target <= moment:
            target += timedelta(seconds=interval)
        return max(0.0, (target - moment).total_seconds())

    def _roll_trading_day(self, today: date) -> None:
        """Notify on a new trading day so callers can refresh the master."""
        if self._current_trading_day == today:
            return
        self._current_trading_day = today
        self._fingerprints.clear()
        if self.on_new_trading_day is None:
            return
        try:
            self.on_new_trading_day(today)
        except Exception as exc:  # noqa: BLE001 - a refresh failure is not fatal
            logger.error("New-trading-day hook failed for %s: %s", today, exc)

    def _check_stall(self, moment: datetime) -> None:
        """Escalate when nothing has been captured for too long."""
        # Before the first success, measure from recorder start. Measuring from
        # "never" would make the first failed cycle report a full stall window
        # of missing data that had not yet had a chance to exist.
        reference = self._last_success_at or self._started_at
        idle = (moment - reference).total_seconds()
        if idle < self.config.stall_alert_seconds:
            return
        if self._stall_reported:
            return

        self._stall_reported = True
        logger.critical(
            "No option chain stored for %.0f minutes while the market is open. "
            "Recording has effectively stopped and this data cannot be recovered "
            "later. Check the broker session and the log for the underlying error.",
            idle / 60.0,
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def daily_summary(self, trade_date: date | None = None) -> dict[str, Any]:
        """Summarise a day's recording, for the end-of-session log.

        Args:
            trade_date: Day to summarise. Today when ``None``.

        Returns:
            Mapping of per-underlying coverage, gaps, and store totals.
        """
        target = (trade_date or self.clock().date()).isoformat()
        coverage = self.store.coverage(self.config.interval_seconds)
        today = coverage[coverage["trade_date"] == target] if not coverage.empty else coverage

        return {
            "trade_date": target,
            "cycles_run": self.cycles_run,
            "cycles_failed": self.cycles_failed,
            "snapshots_written": self.snapshots_written,
            "legs_written": self.legs_written,
            "per_underlying": today.to_dict("records") if not today.empty else [],
            "gaps": [
                g
                for g in self.store.find_gaps(self.config.interval_seconds)
                if g["trade_date"] == target
            ],
            "store": self.store.stats(),
        }
