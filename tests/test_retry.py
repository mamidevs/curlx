"""
Tests for retry and circuit breaker.
"""

import asyncio

import pytest

from curlx.exceptions import CircuitBreakerOpenError, HttpStatusError, RetryExhaustedError
from curlx.retry import CircuitBreaker, with_retry


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        call_count = 0

        @with_retry(max_attempts=3)
        async def do_it():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert await do_it() == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.05)
        async def do_it():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("boom")
            return "ok"

        assert await do_it() == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self):
        @with_retry(max_attempts=2, min_wait=0.01, max_wait=0.05)
        async def do_it():
            raise ConnectionError("always fails")

        with pytest.raises(RetryExhaustedError):
            await do_it()

    @pytest.mark.asyncio
    async def test_no_retry_on_non_retryable_status(self):
        call_count = 0

        @with_retry(max_attempts=3)
        async def do_it():
            nonlocal call_count
            call_count += 1
            raise HttpStatusError(404, "not found")

        with pytest.raises(RetryExhaustedError):
            await do_it()
        assert call_count == 1

    def test_sync_retry(self):
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.05)
        def do_it():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("boom")
            return "ok"

        assert do_it() == "ok"
        assert call_count == 3


class TestCircuitBreaker:
    def test_closed_allows_calls(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1)

        def ok():
            return "success"

        assert cb.call(ok) == "success"
        assert cb.state == "CLOSED"

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)

        def fail():
            raise ValueError("err")

        with pytest.raises(ValueError):
            cb.call(fail)
        with pytest.raises(ValueError):
            cb.call(fail)

        with pytest.raises(CircuitBreakerOpenError):
            cb.call(fail)

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        def fail():
            raise ValueError("err")

        with pytest.raises(ValueError):
            cb.call(fail)

        assert cb.state == "OPEN"
        asyncio.run(asyncio.sleep(0.05))
        assert cb.state == "HALF_OPEN"

    def test_reset(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)

        def fail():
            raise ValueError("err")

        with pytest.raises(ValueError):
            cb.call(fail)
        assert cb.state == "OPEN"

        cb.reset()
        assert cb.state == "CLOSED"
        assert cb.call(lambda: "ok") == "ok"
