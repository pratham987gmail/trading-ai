"""Thread-safe in-memory cache and its background refresh loop.

Design decisions and why.

**Readers never block on the network.** The updater writes complete snapshots
under a lock; endpoints take the lock only to copy a reference out. An HTTP
request therefore cannot be delayed by a slow broker call, which keeps
``/health`` answerable during an outage.

**Stale data is served, loudly.** The charter forbids *acting* on stale data but
says nothing against showing it. Crashing or blanking the cache on a failed poll
destroys context an operator needs. So every payload carries ``age_seconds`` and
``is_stale``, and :meth:`MarketCache.assert_fresh` is the gate that analysis code
must call before generating a signal.

**Section-level updates.** A quote failure must not blank the account section.
Each section carries its own timestamp and error, so partial degradation stays
partial.

**Poll rate follows the session.** Five seconds while the market is open, sixty
otherwise, and candles are not fetched outside the session at all, since they
cannot change. That is the difference between roughly 4,600 quote calls in a
trading day and the same number again overnight for identical data.

Example:
    >>> cache = MarketCache(stale_threshold=60)
    >>> cache.snapshot()["metadata"]["status"]
    'INITIALISING'
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from config.instruments import CORE_INSTRUMENTS, DEFAULT_CANDLE_SERIES, DEFAULT_WATCHLIST
from core.data_fetcher import DataFetcher
from core.exceptions import CacheStaleError
from core.market_hours import (
    get_market_status,
    now_ist,
    poll_interval_seconds,
    should_fetch_candles,
)

logger = logging.getLogger(__name__)

CacheStatus = Literal["INITIALISING", "CONNECTED", "DEGRADED", "STALE", "ERROR"]

# Candle series are refreshed less often than quotes. A 5-minute bar does not
# change meaningfully every 5 seconds, and candle calls carry the tightest rate
# limit of the endpoints in use.
CANDLE_REFRESH_SECONDS = 60

# Account state changes only on fills, so it is polled on a slower cadence.
ACCOUNT_REFRESH_SECONDS = 15


class MarketCache:
    """Thread-safe store for the latest market, account, and candle snapshot.

    Args:
        stale_threshold: Seconds after which data is flagged stale.
    """

    def __init__(self, stale_threshold: int = 60) -> None:
        """Initialise an empty cache."""
        self._lock = threading.RLock()
        self._stale_threshold = stale_threshold

        self._status: CacheStatus = "INITIALISING"
        self._last_update: datetime | None = None
        self._last_success: datetime | None = None
        self._update_count = 0
        self._failure_count = 0
        self._consecutive_failures = 0

        self._market: dict[str, Any] = {}
        self._account: dict[str, Any] = {}
        self._positions: list[dict[str, Any]] = []
        self._orders: list[dict[str, Any]] = []
        self._holdings: list[dict[str, Any]] = []
        self._candles: dict[str, pd.DataFrame] = {}
        self._levels: dict[str, Any] = {}

        self._section_updated: dict[str, datetime] = {}
        self._section_errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Freshness
    # ------------------------------------------------------------------

    @property
    def age_seconds(self) -> float | None:
        """Seconds since the last successful update, or ``None`` if never."""
        with self._lock:
            if self._last_success is None:
                return None
            return (now_ist() - self._last_success).total_seconds()

    @property
    def is_stale(self) -> bool:
        """True when data is older than the threshold or absent entirely."""
        age = self.age_seconds
        return age is None or age > self._stale_threshold

    @property
    def status(self) -> CacheStatus:
        """Current cache status, promoted to ``STALE`` when data has aged out."""
        with self._lock:
            if self._status == "CONNECTED" and self.is_stale:
                return "STALE"
            return self._status

    def assert_fresh(self) -> None:
        """Raise when the cache is too old to act on.

        Analysis and order paths must call this first. Display paths must not,
        so that an operator can still see the last known state.

        Raises:
            CacheStaleError: When data exceeds the staleness threshold.
        """
        age = self.age_seconds
        if age is None:
            raise CacheStaleError(
                "No market data has been successfully fetched yet",
                age_seconds=None,
                threshold_seconds=self._stale_threshold,
            )
        if age > self._stale_threshold:
            raise CacheStaleError(
                f"Market data is {age:.0f}s old and cannot be used for analysis",
                age_seconds=age,
                threshold_seconds=self._stale_threshold,
            )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def set_status(self, status: CacheStatus, error: str | None = None) -> None:
        """Set the overall cache status.

        Args:
            status: New status.
            error: Optional error description recorded against ``"global"``.
        """
        with self._lock:
            self._status = status
            if error:
                self._section_errors["global"] = error

    def update_section(self, name: str, value: Any, error: str | None = None) -> None:
        """Replace one cache section.

        A ``None`` value with an error records the failure and leaves the
        previous value intact, so one failed section does not blank the others.

        Args:
            name: One of ``market``, ``account``, ``positions``, ``orders``,
                ``holdings``, ``levels``.
            value: New section value, or ``None`` to record a failure only.
            error: Failure description.
        """
        with self._lock:
            if error:
                self._section_errors[name] = error
            else:
                self._section_errors.pop(name, None)

            if value is None:
                return

            if name == "market":
                self._market = value
            elif name == "account":
                self._account = value
            elif name == "positions":
                self._positions = value
            elif name == "orders":
                self._orders = value
            elif name == "holdings":
                self._holdings = value
            elif name == "levels":
                self._levels = value
            else:
                logger.warning("Unknown cache section '%s'; ignored", name)
                return

            self._section_updated[name] = now_ist()

    def update_candles(self, symbol: str, interval: str, frame: pd.DataFrame) -> None:
        """Store a candle series.

        Args:
            symbol: Instrument shorthand.
            interval: Canonical interval name.
            frame: OHLCV frame indexed by timestamp.
        """
        if frame is None or frame.empty:
            return
        with self._lock:
            self._candles[f"{symbol.upper()}:{interval.upper()}"] = frame
            self._section_updated[f"candles:{symbol.upper()}:{interval.upper()}"] = now_ist()

    def mark_update(self, success: bool) -> None:
        """Record the outcome of a refresh cycle.

        Args:
            success: Whether the cycle produced any usable data.
        """
        with self._lock:
            self._last_update = now_ist()
            self._update_count += 1
            if success:
                self._last_success = self._last_update
                self._consecutive_failures = 0
            else:
                self._failure_count += 1
                self._consecutive_failures += 1

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_candles(self, symbol: str, interval: str) -> pd.DataFrame | None:
        """Return a cached candle series.

        Args:
            symbol: Instrument shorthand.
            interval: Canonical interval name.

        Returns:
            A copy of the frame, or ``None`` when not cached. A copy is
            returned so a caller cannot mutate cached state.
        """
        with self._lock:
            frame = self._candles.get(f"{symbol.upper()}:{interval.upper()}")
            return frame.copy() if frame is not None else None

    def metadata(self) -> dict[str, Any]:
        """Return cache metadata without payloads.

        Returns:
            Mapping describing status, freshness, counters, and section state.
        """
        with self._lock:
            age = self.age_seconds
            return {
                "status": self.status,
                "last_update": self._last_update.isoformat() if self._last_update else None,
                "last_success": self._last_success.isoformat() if self._last_success else None,
                "age_seconds": round(age, 1) if age is not None else None,
                "is_stale": self.is_stale,
                "stale_threshold_seconds": self._stale_threshold,
                "update_count": self._update_count,
                "failure_count": self._failure_count,
                "consecutive_failures": self._consecutive_failures,
                "sections_updated": {
                    name: ts.isoformat() for name, ts in self._section_updated.items()
                },
                "section_errors": dict(self._section_errors),
                "cached_candle_series": sorted(self._candles),
            }

    def snapshot(self, include_candles: bool = False, candle_bars: int = 50) -> dict[str, Any]:
        """Return a complete point-in-time snapshot.

        Args:
            include_candles: Include candle series as records.
            candle_bars: Trailing bars per series when included.

        Returns:
            Mapping with ``metadata``, ``market_status``, ``market``,
            ``account``, ``positions``, ``orders``, ``holdings``, ``levels``,
            and optionally ``candles``.
        """
        with self._lock:
            payload: dict[str, Any] = {
                "metadata": self.metadata(),
                "market_status": get_market_status(),
                "market": copy.deepcopy(self._market),
                "account": copy.deepcopy(self._account),
                "positions": copy.deepcopy(self._positions),
                "orders": copy.deepcopy(self._orders),
                "holdings": copy.deepcopy(self._holdings),
                "levels": copy.deepcopy(self._levels),
            }

            if include_candles:
                series: dict[str, list[dict[str, Any]]] = {}
                for key, frame in self._candles.items():
                    tail = frame.tail(candle_bars)
                    series[key] = [
                        {
                            "timestamp": index.isoformat(),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": (int(row["volume"]) if pd.notna(row.get("volume")) else None),
                        }
                        for index, row in tail.iterrows()
                    ]
                payload["candles"] = series

            return payload


class CacheUpdater:
    """Background refresh loop for :class:`MarketCache`.

    Runs as an asyncio task. The SmartAPI SDK is synchronous, so every fetch is
    dispatched through ``asyncio.to_thread`` and the event loop stays responsive
    while a call blocks or a rate limiter sleeps.

    Args:
        cache: The cache to populate.
        fetcher: Data source.
        active_interval: Poll seconds while the market is open.
        idle_interval: Poll seconds otherwise.
        candle_bars: Bars to keep per series.
        watchlist: Core instrument shorthands to quote.
        candle_series: ``(symbol, interval)`` pairs to keep warm.
    """

    def __init__(
        self,
        cache: MarketCache,
        fetcher: DataFetcher,
        active_interval: int = 5,
        idle_interval: int = 60,
        candle_bars: int = 200,
        watchlist: tuple[str, ...] = DEFAULT_WATCHLIST,
        candle_series: tuple[tuple[str, str], ...] = DEFAULT_CANDLE_SERIES,
    ) -> None:
        """Initialise the updater without starting it."""
        self.cache = cache
        self.fetcher = fetcher
        self.active_interval = active_interval
        self.idle_interval = idle_interval
        self.candle_bars = candle_bars
        self.watchlist = watchlist
        self.candle_series = candle_series

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_candle_refresh: float = 0.0
        self._last_account_refresh: float = 0.0
        self._levels_session: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the refresh loop. Idempotent."""
        if self._task is not None and not self._task.done():
            logger.debug("Cache updater already running")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="cache-updater")
        logger.info(
            "Cache updater started (active=%ss, idle=%ss)",
            self.active_interval,
            self.idle_interval,
        )

    async def stop(self) -> None:
        """Signal the loop to stop and wait for it to finish."""
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=15.0)
        except TimeoutError:
            logger.warning("Cache updater did not stop in time; cancelling")
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("Cache updater stopped")

    @property
    def is_running(self) -> bool:
        """True while the loop task is alive."""
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Refresh until stopped.

        The loop body is fully guarded: an unhandled exception here would kill
        the task silently and freeze the cache while the server kept reporting
        healthy, so every cycle catches broadly and continues.
        """
        while not self._stop.is_set():
            interval = poll_interval_seconds(self.active_interval, self.idle_interval)
            try:
                await self._refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                logger.error("Cache refresh cycle failed: %s", exc, exc_info=True)
                self.cache.mark_update(success=False)
                self.cache.set_status("ERROR", str(exc))

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _refresh_once(self) -> None:
        """Run one refresh cycle across all due sections."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        any_success = False

        # -- Quotes: every cycle -----------------------------------------
        try:
            market = await asyncio.to_thread(self.fetcher.fetch_index_snapshot, self.watchlist)
            if market:
                self.cache.update_section("market", market)
                any_success = True
            else:
                self.cache.update_section(
                    "market", None, error="No index quotes returned this cycle"
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Market quote refresh failed: %s", exc)
            self.cache.update_section("market", None, error=str(exc))

        # -- Account: slower cadence -------------------------------------
        if now - self._last_account_refresh >= ACCOUNT_REFRESH_SECONDS:
            self._last_account_refresh = now
            try:
                funds = await asyncio.to_thread(self.fetcher.fetch_funds)
                if funds:
                    self.cache.update_section("account", funds)
                    any_success = True
                else:
                    self.cache.update_section("account", None, error="Funds unavailable")

                positions = await asyncio.to_thread(self.fetcher.fetch_positions)
                self.cache.update_section("positions", positions)

                orders = await asyncio.to_thread(self.fetcher.fetch_orders)
                self.cache.update_section("orders", orders)
            except Exception as exc:  # noqa: BLE001
                logger.error("Account refresh failed: %s", exc)
                self.cache.update_section("account", None, error=str(exc))

        # -- Candles: session hours only, slower cadence ------------------
        if should_fetch_candles() and now - self._last_candle_refresh >= CANDLE_REFRESH_SECONDS:
            self._last_candle_refresh = now
            for symbol, interval in self.candle_series:
                if self._stop.is_set():
                    break
                try:
                    frame = await asyncio.to_thread(
                        self.fetcher.fetch_candles, symbol, interval, self.candle_bars
                    )
                    if frame is not None and not frame.empty:
                        self.cache.update_candles(symbol, interval, frame)
                        any_success = True
                except Exception as exc:  # noqa: BLE001
                    logger.error("Candle refresh failed for %s %s: %s", symbol, interval, exc)

        # -- Previous-day levels: once per calendar day --------------------
        # Keyed on the IST date so the levels roll over automatically at the
        # start of each session without a restart.
        today = now_ist().date().isoformat()
        if self._levels_session != today:
            try:
                levels: dict[str, Any] = {}
                for key in ("NIFTY", "BANKNIFTY"):
                    if key not in CORE_INSTRUMENTS:
                        continue
                    computed = await asyncio.to_thread(self.fetcher.fetch_previous_day_levels, key)
                    if computed:
                        levels[key] = computed
                if levels:
                    self.cache.update_section("levels", levels)
                    self._levels_session = today
            except Exception as exc:  # noqa: BLE001
                logger.debug("Previous-day level computation deferred: %s", exc)

        self.cache.mark_update(success=any_success)
        if any_success:
            self.cache.set_status("CONNECTED")
        elif self.fetcher.client.is_connected:
            self.cache.set_status("DEGRADED", "Connected but no data returned this cycle")
        else:
            self.cache.set_status(
                "DEGRADED", self.fetcher.client.last_error or "Broker session unavailable"
            )
