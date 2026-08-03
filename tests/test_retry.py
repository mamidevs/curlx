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
        """
        A non-retryable failure must surface UNCHANGED.

        This used to assert `RetryExhaustedError`, which was wrong twice over:
        nothing was ever retried (call_count == 1), and the wrapping meant a
        caller writing `except HttpStatusError` never fired.
        """
        call_count = 0

        @with_retry(max_attempts=3)
        async def do_it():
            nonlocal call_count
            call_count += 1
            raise HttpStatusError(404, "not found")

        with pytest.raises(HttpStatusError) as excinfo:
            await do_it()
        assert excinfo.value.status_code == 404
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_non_retryable_exception_is_not_wrapped(self):
        """A bug in the caller's own code must not masquerade as a retry failure."""

        @with_retry(max_attempts=3)
        async def do_it():
            raise ValueError("caller bug")

        with pytest.raises(ValueError, match="caller bug"):
            await do_it()

    @pytest.mark.asyncio
    async def test_reported_attempt_count_is_real(self):
        """`attempts` used to always report max_attempts regardless of reality."""
        calls = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
        async def do_it():
            nonlocal calls
            calls += 1
            raise ConnectionError("down")

        with pytest.raises(RetryExhaustedError) as excinfo:
            await do_it()
        assert excinfo.value.attempts == calls == 3

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
