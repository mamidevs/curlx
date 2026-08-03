"""
Token-bucket rate limiting shared by sync and async callers.

This is a *rate* limiter (requests per unit of time), not a concurrency limiter.
Bounding how many requests are in flight at once - what an ``asyncio.Semaphore``
does - says nothing about how fast they are issued, which is the number servers
actually measure when they decide to ban a client.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass

from curlx.utils import get_logger

logger = get_logger("curlx.ratelimit")

# Bucket key used when per-host limiting is off, or when the caller did not
# supply a host. "*" cannot collide with a real hostname.
_SHARED_KEY = "*"


@dataclass
class _Bucket:
    """Mutable state of one token bucket."""

    tokens: float
    updated_at: float


class RateLimiter:
    """
    Classic token bucket, usable from both threads and coroutines.

    Tokens refill continuously at ``requests_per_second`` up to a ceiling of
    ``burst``; every acquire costs one token and sleeps only for the shortfall.

    Waiting is implemented by *reservation*: a caller deducts its token
    immediately - letting the balance go negative - and sleeps for exactly as
    long as that debt takes to refill. Each waiter therefore gets its own wake-up
    time and is served in arrival order. The naive alternative (peek, sleep,
    retry) wakes every waiter simultaneously to fight over a single token, which
    burns CPU and can starve individual callers indefinitely.

    Under sustained overload the balance drifts unboundedly negative and waits
    grow without limit. That is correct queueing behaviour rather than a leak,
    but it does mean this class applies backpressure by blocking; it never drops
    or rejects a request.

    Thread safety: a single ``threading.Lock`` guards all bucket state. The
    critical section contains no ``await`` and no sleep, so it is uncontended in
    practice, and it is the only primitive that can serialise a real OS thread
    against a coroutine - an ``asyncio.Lock`` protects tasks from each other but
    offers no protection at all against another thread mutating the same bucket.

    Example:
        >>> limiter = RateLimiter(10, burst=5, per_host=True)
        >>> limiter.acquire("example.com")
        >>> await limiter.acquire_async("example.com")  # doctest: +SKIP
    """

    def __init__(
        self,
        requests_per_second: float,
        *,
        burst: int | None = None,
        per_host: bool = False,
    ) -> None:
        """
        Args:
            requests_per_second: Sustained refill rate. Must be finite and > 0.
            burst: Bucket capacity, i.e. how many requests may be issued
                back-to-back after an idle period. Defaults to
                ``ceil(requests_per_second)``, never below 1.
            per_host: Keep an independent bucket per host instead of one shared
                budget. Buckets are created lazily on first use.

        Raises:
            ValueError: If the rate is not a finite positive number, or if an
                explicit ``burst`` is below 1.
        """
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError(
                f"requests_per_second must be a finite positive number, got {requests_per_second!r}"
            )

        capacity = math.ceil(requests_per_second) if burst is None else int(burst)
        if capacity < 1:
            if burst is None:
                # Unreachable for a positive rate, but keeps the floor explicit.
                capacity = 1
            else:
                raise ValueError(f"burst must be >= 1, got {burst!r}")

        self.requests_per_second = float(requests_per_second)
        self.burst = capacity
        self.per_host = per_host

        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def acquire(self, host: str | None = None) -> None:
        """
        Block until one token is available, then consume it.

        Args:
            host: Bucket selector. Ignored unless ``per_host`` is enabled;
                ``None`` always maps to the shared bucket.
        """
        key = self._key(host)
        wait = self._reserve(key)
        if wait <= 0:
            return

        logger.debug("Rate limited: sleeping %.3fs | key=%s", wait, key)
        try:
            time.sleep(wait)
        except BaseException:
            # KeyboardInterrupt mid-sleep: hand the reserved token back so the
            # bucket does not stay in debt for a request that never happened.
            self._refund(key)
            raise

    async def acquire_async(self, host: str | None = None) -> None:
        """
        Await until one token is available, then consume it.

        Args:
            host: Bucket selector. Ignored unless ``per_host`` is enabled;
                ``None`` always maps to the shared bucket.
        """
        key = self._key(host)
        wait = self._reserve(key)
        if wait <= 0:
            return

        logger.debug("Rate limited: sleeping %.3fs | key=%s", wait, key)
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            self._refund(key)
            raise

    def reset(self) -> None:
        """Drop every bucket, so the next acquire starts from a full one."""
        with self._lock:
            self._buckets.clear()

    def _key(self, host: str | None) -> str:
        if not self.per_host:
            return _SHARED_KEY
        return host or _SHARED_KEY

    def _reserve(self, key: str) -> float:
        """
        Deduct one token and return how long the caller must sleep for it.

        Runs entirely inside the lock and never sleeps, so the lock is held for
        a few microseconds regardless of how long the returned wait is.
        """
        with self._lock:
            # Sampled inside the lock: a read taken outside could be overtaken
            # by another thread, and writing the stale value back would rewind
            # updated_at and let the next refill credit the same window twice.
            now = time.monotonic()

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.burst), updated_at=now)
                self._buckets[key] = bucket

            elapsed = now - bucket.updated_at
            if elapsed > 0:
                # The ceiling applies to the refill only; a bucket in debt
                # climbs back toward zero at the same rate.
                bucket.tokens = min(
                    float(self.burst),
                    bucket.tokens + elapsed * self.requests_per_second,
                )
            bucket.updated_at = now

            bucket.tokens -= 1.0
            if bucket.tokens >= 0:
                return 0.0
            return -bucket.tokens / self.requests_per_second

    def _refund(self, key: str) -> None:
        """Return a reserved token after an interrupted or cancelled wait."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                bucket.tokens = min(float(self.burst), bucket.tokens + 1.0)

    def __repr__(self) -> str:
        return (
            f"RateLimiter(requests_per_second={self.requests_per_second}, "
            f"burst={self.burst}, per_host={self.per_host})"
        )
