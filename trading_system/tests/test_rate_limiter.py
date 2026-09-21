"""Tests for the token-bucket rate limiter.

Previously untested, flagged in the repository audit as a real gap: this is
what stands between the paper (and eventually live) trading loop and an Angel
One rate-limit lockout. Timing-sensitive assertions use generous tolerances
and short durations to stay fast and non-flaky under CI load.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.rate_limiter import RateLimiterRegistry, TokenBucket, build_registry_from_settings
from config.settings import Settings


class TestTokenBucketBasics:
    def test_rejects_non_positive_rate(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=0)
        with pytest.raises(ValueError):
            TokenBucket(rate=-1.0)

    def test_starts_full(self) -> None:
        bucket = TokenBucket(rate=5.0, capacity=5)
        assert bucket.available_tokens == pytest.approx(5.0)

    def test_try_acquire_succeeds_while_tokens_remain(self) -> None:
        bucket = TokenBucket(rate=10.0, capacity=3)
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is True

    def test_try_acquire_fails_once_exhausted(self) -> None:
        bucket = TokenBucket(rate=1.0, capacity=1)
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False  # no blocking, no refill wait

    def test_capacity_defaults_from_rate(self) -> None:
        bucket = TokenBucket(rate=8.0)
        assert bucket.capacity >= 8

    def test_tokens_refill_over_time(self) -> None:
        bucket = TokenBucket(rate=20.0, capacity=1)  # 1 token, refills fast
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False
        time.sleep(0.1)  # 20/s rate -> ~2 tokens accrued, clamped to capacity
        assert bucket.try_acquire() is True

    def test_refill_never_exceeds_capacity(self) -> None:
        bucket = TokenBucket(rate=100.0, capacity=2)
        time.sleep(0.1)
        assert bucket.available_tokens <= 2.0


class TestBlockingAcquire:
    def test_acquire_blocks_until_a_token_is_available(self) -> None:
        bucket = TokenBucket(rate=20.0, capacity=1)
        bucket.try_acquire()  # drain it

        started = time.monotonic()
        result = bucket.acquire(timeout=1.0)
        elapsed = time.monotonic() - started

        assert result is True
        assert elapsed < 1.0  # got it well before the timeout, via refill

    def test_acquire_times_out_when_starved(self) -> None:
        bucket = TokenBucket(rate=0.5, capacity=1)  # very slow refill
        bucket.try_acquire()  # drain it

        started = time.monotonic()
        result = bucket.acquire(timeout=0.15)
        elapsed = time.monotonic() - started

        assert result is False
        assert elapsed < 1.0  # respected the short timeout, did not hang

    def test_acquire_with_no_timeout_eventually_succeeds(self) -> None:
        bucket = TokenBucket(rate=50.0, capacity=1)
        bucket.try_acquire()
        assert bucket.acquire(timeout=None) is True

    def test_requesting_more_than_capacity_is_clamped_not_a_deadlock(self) -> None:
        bucket = TokenBucket(rate=10.0, capacity=2)
        # Would never be satisfiable without clamping; must not hang forever.
        result = bucket.acquire(tokens=1000.0, timeout=0.5)
        assert result is True

    def test_stats_record_throttled_waits(self) -> None:
        bucket = TokenBucket(rate=20.0, capacity=1)
        bucket.try_acquire()
        bucket.acquire(timeout=1.0)
        assert bucket.stats.throttled >= 1
        assert bucket.stats.total_wait_seconds > 0


class TestThreadSafety:
    def test_concurrent_acquires_never_oversubscribe_capacity(self) -> None:
        """Ten threads racing for five tokens must never let more than five through."""
        bucket = TokenBucket(rate=0.001, capacity=5)  # effectively no refill in this window
        successes = []
        lock = threading.Lock()

        def worker() -> None:
            got = bucket.try_acquire()
            with lock:
                successes.append(got)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert sum(1 for s in successes if s) == 5


class TestRegistry:
    def test_acquire_uses_the_named_bucket(self) -> None:
        registry = RateLimiterRegistry()
        registry.register("quote", rate=100.0, capacity=1)
        assert registry.acquire("quote") is True

    def test_unregistered_name_fails_open_rather_than_blocking(self) -> None:
        """A missing limiter must not deadlock a fetch; it proceeds, logged."""
        registry = RateLimiterRegistry()
        assert registry.acquire("nonexistent_bucket") is True

    def test_each_named_bucket_is_independent(self) -> None:
        registry = RateLimiterRegistry()
        registry.register("ltp", rate=1.0, capacity=1)
        registry.register("order", rate=1.0, capacity=1)

        assert registry.acquire("ltp", timeout=0) is True
        # Draining "ltp" must not affect "order".
        assert registry.acquire("order", timeout=0) is True

    def test_status_reports_every_bucket(self) -> None:
        registry = RateLimiterRegistry()
        registry.register("ltp", rate=5.0)
        registry.register("order", rate=1.0)
        status = registry.status()
        assert set(status) == {"ltp", "order"}
        assert "available" in status["ltp"]

    def test_build_from_settings_registers_expected_buckets(self) -> None:
        settings = Settings(_env_file=None)
        registry = build_registry_from_settings(settings)
        # Whatever the exact endpoint names, at least the core groups this
        # client actually throttles must be present and independently usable.
        assert registry.buckets
        for name in registry.buckets:
            assert registry.acquire(name, timeout=0) is True
