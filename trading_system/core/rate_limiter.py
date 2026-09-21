"""Token-bucket rate limiting for SmartAPI calls.

Angel One enforces per-endpoint request ceilings and answers breaches with
"Access denied because of exceeding access rate". Repeated breaches can get an
API key throttled, so the client throttles itself rather than discovering the
limit empirically.

A token bucket is used instead of a fixed window because it permits a short
burst up to the bucket capacity while holding the long-run average at the
configured rate. That matches how quotes are actually consumed: several tokens
fetched back-to-back on a cache tick, then idle.

Both a threading and an asyncio variant are provided. The SmartAPI SDK is
synchronous and its calls are dispatched to a worker thread, so the acquire
happens on that thread and must not block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RateLimitStats:
    """Observed limiter behaviour, for health reporting.

    Attributes:
        acquired: Successful token acquisitions.
        throttled: Acquisitions that had to wait.
        total_wait_seconds: Cumulative time spent waiting.
        max_wait_seconds: Longest single wait.
    """

    acquired: int = 0
    throttled: int = 0
    total_wait_seconds: float = 0.0
    max_wait_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return the stats as a plain mapping."""
        return {
            "acquired": self.acquired,
            "throttled": self.throttled,
            "total_wait_seconds": round(self.total_wait_seconds, 3),
            "max_wait_seconds": round(self.max_wait_seconds, 3),
            "throttle_rate": (round(self.throttled / self.acquired, 4) if self.acquired else 0.0),
        }


class TokenBucket:
    """Thread-safe token bucket.

    Tokens refill continuously at ``rate`` per second up to ``capacity``.
    :meth:`acquire` blocks until a token is available.

    Args:
        rate: Sustained requests per second. Must be positive.
        capacity: Burst size. Defaults to ``max(1, ceil(rate))``.
        name: Label used in log messages.

    Example:
        >>> bucket = TokenBucket(rate=2.0, name="quote")
        >>> bucket.acquire()
        True
        >>> bucket.try_acquire()
        True
    """

    def __init__(self, rate: float, capacity: int | None = None, name: str = "default") -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")

        self.rate = float(rate)
        self.capacity = int(capacity) if capacity else max(1, int(rate + 0.999))
        self.name = name

        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self.stats = RateLimitStats()

    def _refill_locked(self) -> None:
        """Add tokens accrued since the last refill. Caller holds the lock."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Take tokens if available, without blocking.

        Args:
            tokens: Number of tokens required.

        Returns:
            True when the tokens were taken, False when unavailable.
        """
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                self.stats.acquired += 1
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Take tokens, blocking until they are available.

        Args:
            tokens: Number of tokens required.
            timeout: Maximum seconds to wait; ``None`` waits indefinitely.

        Returns:
            True when the tokens were taken, False on timeout.
        """
        if tokens > self.capacity:
            logger.warning(
                "Rate limiter '%s': request for %.1f tokens exceeds capacity %d; clamping",
                self.name,
                tokens,
                self.capacity,
            )
            tokens = float(self.capacity)

        deadline = None if timeout is None else time.monotonic() + timeout
        wait_started = time.monotonic()
        waited = False

        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self.stats.acquired += 1
                    if waited:
                        elapsed = time.monotonic() - wait_started
                        self.stats.throttled += 1
                        self.stats.total_wait_seconds += elapsed
                        self.stats.max_wait_seconds = max(self.stats.max_wait_seconds, elapsed)
                        logger.debug("Rate limiter '%s' throttled for %.3fs", self.name, elapsed)
                    return True
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Rate limiter '%s': timed out after %.1fs waiting for %.1f tokens",
                        self.name,
                        timeout,
                        tokens,
                    )
                    return False
                sleep_for = min(sleep_for, remaining)

            waited = True
            time.sleep(max(sleep_for, 0.001))

    @property
    def available_tokens(self) -> float:
        """Currently available tokens, after an implicit refill."""
        with self._lock:
            self._refill_locked()
            return round(self._tokens, 3)

    def status(self) -> dict[str, Any]:
        """Summarise limiter configuration and behaviour."""
        return {
            "name": self.name,
            "rate_per_second": self.rate,
            "capacity": self.capacity,
            "available": self.available_tokens,
            **self.stats.as_dict(),
        }


class AsyncTokenBucket:
    """Asyncio token bucket, for limiting work on the event loop.

    Mirrors :class:`TokenBucket` but yields to the loop instead of sleeping the
    thread. Use this for limits enforced in async code; use
    :class:`TokenBucket` for limits enforced inside worker threads.

    Args:
        rate: Sustained requests per second.
        capacity: Burst size.
        name: Label used in log messages.
    """

    def __init__(self, rate: float, capacity: int | None = None, name: str = "default") -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self.rate = float(rate)
        self.capacity = int(capacity) if capacity else max(1, int(rate + 0.999))
        self.name = name
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self.stats = RateLimitStats()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Take tokens, awaiting availability.

        Args:
            tokens: Number of tokens required.
        """
        tokens = min(float(tokens), float(self.capacity))
        wait_started = time.monotonic()
        waited = False

        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                if elapsed > 0:
                    self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                    self._last_refill = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self.stats.acquired += 1
                    if waited:
                        spent = time.monotonic() - wait_started
                        self.stats.throttled += 1
                        self.stats.total_wait_seconds += spent
                        self.stats.max_wait_seconds = max(self.stats.max_wait_seconds, spent)
                    return
                sleep_for = (tokens - self._tokens) / self.rate

            waited = True
            await asyncio.sleep(max(sleep_for, 0.001))

    def status(self) -> dict[str, Any]:
        """Summarise limiter configuration and behaviour."""
        return {
            "name": self.name,
            "rate_per_second": self.rate,
            "capacity": self.capacity,
            **self.stats.as_dict(),
        }


@dataclass
class RateLimiterRegistry:
    """Named buckets, one per SmartAPI endpoint group.

    Grouping matters because Angel One's limits are per endpoint. A single
    global bucket would either throttle cheap calls needlessly or overrun the
    tightest limit.

    Example:
        >>> registry = RateLimiterRegistry()
        >>> registry.register("quote", rate=1.0)
        >>> registry.acquire("quote")
        True
    """

    buckets: dict[str, TokenBucket] = field(default_factory=dict)

    def register(self, name: str, rate: float, capacity: int | None = None) -> TokenBucket:
        """Create or replace a named bucket.

        Args:
            name: Endpoint group name.
            rate: Sustained requests per second.
            capacity: Burst size.

        Returns:
            The registered bucket.
        """
        bucket = TokenBucket(rate=rate, capacity=capacity, name=name)
        self.buckets[name] = bucket
        logger.debug("Rate limiter registered: %s at %.2f req/s", name, rate)
        return bucket

    def acquire(self, name: str, tokens: float = 1.0, timeout: float | None = 30.0) -> bool:
        """Acquire from a named bucket.

        An unregistered name is allowed through rather than blocking, since
        failing open on a missing limiter is safer than deadlocking a fetch.
        The omission is logged.

        Args:
            name: Endpoint group name.
            tokens: Tokens required.
            timeout: Maximum wait in seconds.

        Returns:
            True when acquired or when no such bucket exists.
        """
        bucket = self.buckets.get(name)
        if bucket is None:
            logger.warning("No rate limiter registered for '%s'; proceeding unthrottled", name)
            return True
        return bucket.acquire(tokens=tokens, timeout=timeout)

    def status(self) -> dict[str, Any]:
        """Return status for every registered bucket."""
        return {name: bucket.status() for name, bucket in self.buckets.items()}


def build_registry_from_settings(settings: Any) -> RateLimiterRegistry:
    """Construct a registry from validated settings.

    Args:
        settings: A :class:`config.settings.Settings` instance.

    Returns:
        A registry with buckets for quote, ltp, candle, account, and order.
    """
    registry = RateLimiterRegistry()
    registry.register("quote", settings.RATE_LIMIT_QUOTE)
    registry.register("ltp", settings.RATE_LIMIT_LTP)
    registry.register("candle", settings.RATE_LIMIT_CANDLE)
    registry.register("account", settings.RATE_LIMIT_ACCOUNT)
    registry.register("order", settings.RATE_LIMIT_ORDER)
    return registry
