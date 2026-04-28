"""
Retry decorator with circuit breaker support.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from functools import wraps
from typing import Any, Callable, Optional, Type, Union

from curl_cffi import requests as curl_requests

from curlx.exceptions import CircuitBreakerOpenError, HttpStatusError, RetryExhaustedError
from curlx.utils import get_logger, jitter

logger = get_logger("curlx.retry")

DEFAULT_RETRYABLE_STATUS_CODES = frozenset(
    {408, 409, 425, 429, 500, 502, 503, 504}
)
DEFAULT_RETRYABLE_EXCEPTIONS = (
    curl_requests.errors.RequestsError,
    curl_requests.errors.CurlError,
    ConnectionError,
    TimeoutError,
    OSError,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpStatusError):
        return exc.status_code in DEFAULT_RETRYABLE_STATUS_CODES
    return isinstance(exc, DEFAULT_RETRYABLE_EXCEPTIONS)


def with_retry(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 10.0,
    retryable_exceptions: Optional[tuple] = None,
    retryable_status_codes: Optional[frozenset] = None,
    on_retry: Optional[Callable[[Exception, int], None]] = None,
    reraise: bool = True,
) -> Callable:
    """
    Decorator that retries an async function on failure.

    Args:
        max_attempts: Maximum number of attempts (including the first).
        min_wait: Minimum wait seconds between retries.
        max_wait: Maximum wait seconds between retries.
        retryable_exceptions: Tuple of exception classes to retry on.
        retryable_status_codes: HTTP status codes to retry.
        on_retry: Callback(exc, attempt_number) called on each retry.
        reraise: If True, re-raise the last exception after exhaustion.
    """

    def decorator(func: Callable) -> Callable:
        exc_types = retryable_exceptions or DEFAULT_RETRYABLE_EXCEPTIONS
        status_codes = retryable_status_codes or DEFAULT_RETRYABLE_STATUS_CODES

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    should_retry = False
                    if isinstance(exc, HttpStatusError):
                        should_retry = exc.status_code in status_codes
                    elif isinstance(exc, exc_types):
                        should_retry = True

                    if not should_retry or attempt >= max_attempts:
                        break

                    wait = jitter(min(min_wait * (2 ** (attempt - 1)), max_wait))
                    if on_retry:
                        try:
                            on_retry(exc, attempt)
                        except Exception:
                            pass
                    logger.warning(
                        "Retry %d/%d for %s in %.2fs | exc=%s",
                        attempt,
                        max_attempts,
                        func.__name__,
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)

            if reraise and last_exc is not None:
                raise RetryExhaustedError(
                    str(last_exc),
                    last_exception=last_exc,
                    attempts=max_attempts,
                ) from last_exc
            return None

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    should_retry = False
                    if isinstance(exc, HttpStatusError):
                        should_retry = exc.status_code in status_codes
                    elif isinstance(exc, exc_types):
                        should_retry = True

                    if not should_retry or attempt >= max_attempts:
                        break

                    wait = jitter(min(min_wait * (2 ** (attempt - 1)), max_wait))
                    if on_retry:
                        try:
                            on_retry(exc, attempt)
                        except Exception:
                            pass
                    logger.warning(
                        "Retry %d/%d for %s in %.2fs | exc=%s",
                        attempt,
                        max_attempts,
                        func.__name__,
                        wait,
                        exc,
                    )
                    time.sleep(wait)

            if reraise and last_exc is not None:
                raise RetryExhaustedError(
                    str(last_exc),
                    last_exception=last_exc,
                    attempts=max_attempts,
                ) from last_exc
            return None

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


class CircuitBreaker:
    """
    Simple in-memory circuit breaker.

    States:
        CLOSED   – requests flow normally.
        OPEN     – requests are blocked immediately.
        HALF_OPEN – after timeout, one probe request is allowed.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._failures = 0
        self._last_failure_time: Optional[float] = None
        self._state = "CLOSED"
        self._half_open_calls = 0

    @property
    def state(self) -> str:
        if self._state == "OPEN":
            if self._last_failure_time and (
                time.time() - self._last_failure_time >= self.recovery_timeout
            ):
                self._state = "HALF_OPEN"
                self._half_open_calls = 0
        return self._state

    def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        st = self.state
        if st == "OPEN":
            raise CircuitBreakerOpenError("Circuit breaker is OPEN")

        if st == "HALF_OPEN" and self._half_open_calls >= self.half_open_max_calls:
            raise CircuitBreakerOpenError("Circuit breaker HALF_OPEN limit reached")

        if st == "HALF_OPEN":
            self._half_open_calls += 1

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise exc

    async def call_async(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        st = self.state
        if st == "OPEN":
            raise CircuitBreakerOpenError("Circuit breaker is OPEN")

        if st == "HALF_OPEN" and self._half_open_calls >= self.half_open_max_calls:
            raise CircuitBreakerOpenError("Circuit breaker HALF_OPEN limit reached")

        if st == "HALF_OPEN":
            self._half_open_calls += 1

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise exc

    def _on_success(self) -> None:
        self._failures = 0
        self._state = "CLOSED"
        self._half_open_calls = 0

    def _on_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = "OPEN"
            logger.error(
                "Circuit breaker OPENED after %d failures", self._failures
            )

    def reset(self) -> None:
        self._failures = 0
        self._last_failure_time = None
        self._state = "CLOSED"
        self._half_open_calls = 0
