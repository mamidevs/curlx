"""
Retry decorator with circuit breaker support.

The retry predicate is driven by the ``TransportError`` subtree in
:mod:`curlx.exceptions`. ``client.py`` funnels curl_cffi failures through
:func:`curlx.exceptions.map_transport_error` before they reach here, so retry
normally sees curlx types; the raw curl_cffi and builtin equivalents are kept in
the defaults anyway for callers who decorate a bare curl_cffi call.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import random
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, NamedTuple, ParamSpec, TypeVar, cast

from curl_cffi.requests import exceptions as _curl_exc

from curlx.exceptions import (
    CertificateError,
    CircuitBreakerOpenError,
    ConnectionFailedError,
    HttpStatusError,
    ProxyError,
    ProxyPoolExhaustedError,
    RetryExhaustedError,
    TimeoutExceededError,
    TlsError,
)
from curlx.utils import get_logger

logger = get_logger("curlx.retry")

P = ParamSpec("P")
R = TypeVar("R")

#: Sentinel distinguishing "no fallback configured" from ``fallback=None``.
_UNSET: Any = object()

DEFAULT_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Transient failures worth another attempt. Deliberately *not* ``OSError``:
#: that subsumes the precise entries below and would also retry
#: ``FileNotFoundError`` / ``PermissionError`` raised by the caller's own code.
#: ``TooManyRedirectsError`` and ``ImpersonationError`` are absent because they
#: are deterministic - a second identical request fails identically.
DEFAULT_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionFailedError,  # includes DNSResolutionError
    TimeoutExceededError,
    TlsError,  # transient handshake failures; see NON_RETRYABLE_EXCEPTIONS
    ProxyError,  # a single bad proxy; see NON_RETRYABLE_EXCEPTIONS
    # Raw curl_cffi types, for callers who bypass map_transport_error. These
    # derive from OSError rather than the builtins below, so both sets matter.
    _curl_exc.ConnectionError,  # includes DNSError, SSLError
    _curl_exc.Timeout,
    _curl_exc.ProxyError,
    # Builtins raised outside curl_cffi.
    ConnectionError,
    TimeoutError,
)

#: Permanent failures nested *inside* the broad categories above. A retry cannot
#: change the outcome: the certificate is still untrusted, the pool is still
#: empty. Only consulted when the caller accepts the default exception tuple -
#: an explicit ``retryable_exceptions`` argument is honoured verbatim.
NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ProxyPoolExhaustedError,
    CertificateError,
    _curl_exc.CertificateVerifyError,
)


def is_retryable(
    exc: BaseException,
    *,
    retryable_exceptions: tuple[type[BaseException], ...] | None = None,
    retryable_status_codes: frozenset[int] | None = None,
) -> bool:
    """
    Decide whether ``exc`` is worth another attempt.

    This is the single predicate used by :func:`with_retry`; it is public so
    callers can reuse the exact same decision outside a decorated function.

    Args:
        exc: The exception raised by the attempt.
        retryable_exceptions: Overrides :data:`DEFAULT_RETRYABLE_EXCEPTIONS`.
            When supplied, the :data:`NON_RETRYABLE_EXCEPTIONS` carve-outs are
            skipped - an explicit request is taken at face value.
        retryable_status_codes: Overrides :data:`DEFAULT_RETRYABLE_STATUS_CODES`
            for :class:`~curlx.exceptions.HttpStatusError`.
    """
    if retryable_exceptions is None:
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        retryable_exceptions = DEFAULT_RETRYABLE_EXCEPTIONS

    if isinstance(exc, HttpStatusError):
        codes = (
            DEFAULT_RETRYABLE_STATUS_CODES
            if retryable_status_codes is None
            else retryable_status_codes
        )
        return exc.status_code in codes

    return isinstance(exc, retryable_exceptions)


class _Decision(NamedTuple):
    """What a wrapper should do after an attempt raised."""

    wait: float | None  # Seconds to sleep before retrying; None means stop.
    # Stop and let the original exception through untouched. Distinct from the
    # public ``reraise`` argument, which is about the exhaustion path.
    propagate: bool


class _RetryPolicy:
    """
    The retry decision logic, shared verbatim by the sync and async wrappers.

    Keeping it here is what lets each wrapper be a ten-line loop whose only
    difference is ``await`` - previously the two copies drifted apart and every
    bug had to be fixed twice.
    """

    def __init__(
        self,
        *,
        func_name: str,
        max_attempts: int,
        min_wait: float,
        max_wait: float,
        retryable_exceptions: tuple[type[BaseException], ...] | None,
        retryable_status_codes: frozenset[int] | None,
        on_retry: Callable[[Exception, int], None] | None,
        respect_retry_after: bool,
        fallback: Any,
    ) -> None:
        self.func_name = func_name
        self.max_attempts = max_attempts
        self.min_wait = min_wait
        self.max_wait = max_wait
        self.retryable_exceptions = retryable_exceptions
        self.retryable_status_codes = retryable_status_codes
        self.on_retry = on_retry
        self.respect_retry_after = respect_retry_after
        self.fallback = fallback

    def decide(self, exc: Exception, attempt: int) -> _Decision:
        """Classify a failed attempt and, when retrying, run the notifications."""
        if not is_retryable(
            exc,
            retryable_exceptions=self.retryable_exceptions,
            retryable_status_codes=self.retryable_status_codes,
        ):
            return _Decision(wait=None, propagate=True)

        if attempt >= self.max_attempts:
            return _Decision(wait=None, propagate=False)

        wait = self._next_wait(exc, attempt)
        self._notify(exc, attempt, wait)
        return _Decision(wait=wait, propagate=False)

    def give_up(self, exc: Exception, attempts: int) -> Any:
        """Handle genuine exhaustion: raise, or return the configured fallback."""
        if self.fallback is _UNSET:
            raise RetryExhaustedError(
                str(exc),
                url=getattr(exc, "url", None),
                last_exception=exc,
                attempts=attempts,
            ) from exc

        logger.error(
            "Retry exhausted for %s after %d attempts, returning fallback | exc=%s",
            self.func_name,
            attempts,
            exc,
        )
        return self.fallback

    def _next_wait(self, exc: Exception, attempt: int) -> float:
        if (
            self.respect_retry_after
            and isinstance(exc, HttpStatusError)
            and exc.retry_after is not None
        ):
            # An explicit server instruction beats any backoff we could compute,
            # and is used as-is (no jitter). Still capped by max_wait so a
            # hostile or misconfigured Retry-After cannot pin the caller.
            return max(0.0, min(float(exc.retry_after), self.max_wait))

        backoff = min(self.min_wait * (2 ** (attempt - 1)), self.max_wait)
        # Full jitter: the whole point is to scatter a synchronised herd, which
        # the +/-10% of utils.jitter does not achieve.
        return random.uniform(0.0, backoff)

    def _notify(self, exc: Exception, attempt: int, wait: float) -> None:
        if self.on_retry is not None:
            try:
                self.on_retry(exc, attempt)
            except Exception:
                # The callback is the caller's code and must not break the
                # retry, but swallowing it silently hides real bugs.
                logger.warning(
                    "on_retry callback raised for %s (attempt %d)",
                    self.func_name,
                    attempt,
                    exc_info=True,
                )
        logger.warning(
            "Retry %d/%d for %s in %.2fs | exc=%s",
            attempt,
            self.max_attempts,
            self.func_name,
            wait,
            exc,
        )


def _callable_name(func: Any) -> str:
    while isinstance(func, functools.partial):
        func = func.func
    return getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or repr(func)


def _is_async_callable(func: Any) -> bool:
    """
    Detect coroutine functions behind ``functools.partial`` and async ``__call__``.

    A bare ``inspect.iscoroutinefunction`` misses class instances whose
    ``__call__`` is async, which would silently hand them the blocking
    ``time.sleep`` wrapper inside an event loop.
    """
    while isinstance(func, functools.partial):
        func = func.func
    if inspect.iscoroutinefunction(func):
        return True
    # Not a callability test (which is what B004 warns about) - we need the
    # slot itself to ask whether it is a coroutine function.
    call = getattr(type(func), "__call__", None)  # noqa: B004
    return call is not None and inspect.iscoroutinefunction(call)


def with_retry(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 10.0,
    retryable_exceptions: tuple[type[BaseException], ...] | None = None,
    retryable_status_codes: frozenset[int] | None = None,
    on_retry: Callable[[Exception, int], None] | None = None,
    reraise: bool = True,
    respect_retry_after: bool = True,
    fallback: Any = _UNSET,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Retry a sync or async function on transient failures.

    The wrapper is chosen from the wrapped object, so the same decorator works
    on ``def``, ``async def``, ``functools.partial`` and callable instances.

    Only exceptions the retry predicate accepts are retried. Anything else -
    a ``ValueError`` from the caller's own code, a 404 - propagates unchanged,
    so ``except ValueError:`` around a decorated call still works.

    Args:
        max_attempts: Total attempts including the first. Must be >= 1.
        min_wait: Base of the exponential backoff curve. Note this is a base,
            not a floor: with full jitter the actual sleep is uniform in
            ``[0, min(min_wait * 2 ** (attempt - 1), max_wait)]``.
        max_wait: Upper bound on any single sleep, including ``Retry-After``.
        retryable_exceptions: Overrides :data:`DEFAULT_RETRYABLE_EXCEPTIONS`.
        retryable_status_codes: Overrides :data:`DEFAULT_RETRYABLE_STATUS_CODES`.
        on_retry: ``callback(exc, attempt_number)`` invoked before each sleep.
            Exceptions from it are logged and do not abort the retry.
        reraise: Kept for backward compatibility. ``reraise=False`` is only
            accepted together with an explicit ``fallback``.
        respect_retry_after: Honour ``HttpStatusError.retry_after`` (capped by
            ``max_wait``) in preference to the computed backoff.
        fallback: Value returned instead of raising
            :class:`~curlx.exceptions.RetryExhaustedError` once attempts are
            exhausted. Supplying it implies ``reraise=False``.

    Raises:
        ValueError: For an unusable configuration, at decoration time.
        RetryExhaustedError: On exhaustion, unless ``fallback`` was supplied.

    Note:
        Failure must be *observable*. Earlier versions returned ``None`` when
        ``reraise=False``, which a caller cannot tell apart from a successful
        call that legitimately returned ``None``. ``reraise=False`` therefore
        now requires an explicit ``fallback`` sentinel of the caller's choosing.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if not reraise and fallback is _UNSET:
        raise ValueError(
            "with_retry(reraise=False) requires an explicit fallback=<value>. "
            "Returning None on exhaustion is indistinguishable from a successful "
            "call that returned None, so the failure would pass silently."
        )

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        policy = _RetryPolicy(
            func_name=_callable_name(func),
            max_attempts=max_attempts,
            min_wait=min_wait,
            max_wait=max_wait,
            retryable_exceptions=retryable_exceptions,
            retryable_status_codes=retryable_status_codes,
            on_retry=on_retry,
            respect_retry_after=respect_retry_after,
            fallback=fallback,
        )

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return await cast(Callable[..., Awaitable[Any]], func)(*args, **kwargs)
                except Exception as exc:
                    decision = policy.decide(exc, attempt)
                    if decision.propagate:
                        raise
                    if decision.wait is None:
                        return policy.give_up(exc, attempt)
                    await asyncio.sleep(decision.wait)

        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    decision = policy.decide(exc, attempt)
                    if decision.propagate:
                        raise
                    if decision.wait is None:
                        return policy.give_up(exc, attempt)
                    time.sleep(decision.wait)

        wrapper = async_wrapper if _is_async_callable(func) else sync_wrapper
        return cast(Callable[P, R], wrapper)

    return decorator


class CircuitBreaker:
    """
    In-memory circuit breaker with a rolling failure window.

    States:
        CLOSED    - requests flow normally.
        OPEN      - requests are rejected immediately.
        HALF_OPEN - after ``recovery_timeout``, a limited number of probes pass.

    Failures are counted over a sliding time window rather than as a strictly
    consecutive run, so a backend failing half its requests still trips the
    breaker. A success in CLOSED deliberately does *not* clear the window; only
    a successful HALF_OPEN probe does.

    Thread safety: the state machine is guarded by a ``threading.Lock`` that is
    never held while the wrapped callable runs. There is no ``asyncio.Lock`` -
    the async path awaits nothing inside a critical section, so it is already
    atomic and would only be needlessly serialised by one.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        *,
        failure_window: float | None = 60.0,
    ) -> None:
        """
        Args:
            failure_threshold: Failures within ``failure_window`` that open it.
            recovery_timeout: Seconds in OPEN before probes are admitted.
            half_open_max_calls: Concurrent probes allowed while HALF_OPEN.
            failure_window: Length of the sliding window in seconds. ``None``
                counts every failure since the breaker last closed or reset.
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.failure_window = failure_window

        self._lock = threading.Lock()
        self._failures: deque[float] = deque()
        self._last_failure_time: float | None = None
        self._state = "CLOSED"
        self._half_open_calls = 0

    # ------------------------------------------------------------------
    # Observation - pure, safe to poll from metrics code
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        """
        The current state, as an observer sees it.

        Reading this has no side effects. It reports ``HALF_OPEN`` once
        ``recovery_timeout`` has elapsed, but does not *commit* the transition;
        only :meth:`call` / :meth:`call_async` do that. Reading used to perform
        the transition and reset the probe budget, so a metrics scrape handed
        out extra probes.
        """
        with self._lock:
            return self._observed_state()

    @property
    def failure_count(self) -> int:
        """Failures currently inside the rolling window."""
        with self._lock:
            self._prune(time.monotonic())
            return len(self._failures)

    def _observed_state(self) -> str:
        if self._state == "OPEN" and self._recovery_elapsed():
            return "HALF_OPEN"
        return self._state

    def _recovery_elapsed(self) -> bool:
        return (
            self._last_failure_time is not None
            and time.monotonic() - self._last_failure_time >= self.recovery_timeout
        )

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------
    def call(self, func: Callable[..., R], *args: Any, **kwargs: Any) -> R:
        """Run ``func`` through the breaker."""
        self._before_call()
        try:
            result = func(*args, **kwargs)
        except Exception:
            self._after_failure()
            raise
        self._after_success()
        return result

    async def call_async(self, func: Callable[..., Awaitable[R]], *args: Any, **kwargs: Any) -> R:
        """Await ``func`` through the breaker."""
        self._before_call()
        try:
            result = await func(*args, **kwargs)
        except Exception:
            self._after_failure()
            raise
        self._after_success()
        return result

    def reset(self) -> None:
        """Force the breaker back to CLOSED and drop the failure window."""
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------
    # State machine - all of these take the lock, none hold it across user code
    # ------------------------------------------------------------------
    def _before_call(self) -> None:
        with self._lock:
            self._maybe_half_open()
            if self._state == "OPEN":
                raise CircuitBreakerOpenError("Circuit breaker is OPEN")
            if self._state == "HALF_OPEN":
                # Check and increment must be one critical section, or N threads
                # all pass a half_open_max_calls=1 check before any increments.
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerOpenError("Circuit breaker HALF_OPEN limit reached")
                self._half_open_calls += 1

    def _maybe_half_open(self) -> None:
        """Commit the OPEN -> HALF_OPEN transition. Caller holds the lock."""
        if self._state == "OPEN" and self._recovery_elapsed():
            self._state = "HALF_OPEN"
            self._half_open_calls = 0

    def _after_success(self) -> None:
        with self._lock:
            if self._state == "HALF_OPEN":
                # The probe worked. Clearing the window matters: leaving stale
                # failures behind would re-open the breaker on the next error
                # and it would never recover.
                self._close_locked()
                logger.info("Circuit breaker CLOSED after a successful probe")
            # In CLOSED a success does *not* erase recorded failures. Doing so
            # is what limited the old breaker to strictly consecutive failures.

    def _after_failure(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._failures.append(now)
            self._last_failure_time = now
            self._prune(now)

            if self._state == "HALF_OPEN":
                self._state = "OPEN"
                self._half_open_calls = 0
                logger.warning("Circuit breaker re-OPENED after a failed probe")
            elif len(self._failures) >= self.failure_threshold and self._state != "OPEN":
                self._state = "OPEN"
                logger.error(
                    "Circuit breaker OPENED after %d failures within %s",
                    len(self._failures),
                    "the lifetime of the window"
                    if self.failure_window is None
                    else f"{self.failure_window}s",
                )

    def _close_locked(self) -> None:
        self._failures.clear()
        self._last_failure_time = None
        self._state = "CLOSED"
        self._half_open_calls = 0

    def _prune(self, now: float) -> None:
        """Drop failures that have aged out of the window. Caller holds the lock."""
        if self.failure_window is None:
            return
        cutoff = now - self.failure_window
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()


__all__ = [
    "DEFAULT_RETRYABLE_EXCEPTIONS",
    "DEFAULT_RETRYABLE_STATUS_CODES",
    "NON_RETRYABLE_EXCEPTIONS",
    "CircuitBreaker",
    "is_retryable",
    "with_retry",
]
