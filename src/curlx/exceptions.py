"""
Custom exception hierarchy for curlx.
"""

from typing import Optional


class CurlxError(Exception):
    """Base exception for all curlx errors."""

    def __init__(self, message: str, *, url: Optional[str] = None) -> None:
        super().__init__(message)
        self.url = url


class HttpStatusError(CurlxError):
    """Raised when an HTTP response has an unexpected status code."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        url: Optional[str] = None,
        response_body: Optional[bytes] = None,
    ) -> None:
        super().__init__(message, url=url)
        self.status_code = status_code
        self.response_body = response_body

    def __str__(self) -> str:
        return f"HTTP {self.status_code}: {self.args[0]} (url={self.url})"


class RetryExhaustedError(CurlxError):
    """Raised when all retry attempts have been exhausted."""

    def __init__(
        self,
        message: str,
        *,
        url: Optional[str] = None,
        last_exception: Optional[Exception] = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(message, url=url)
        self.last_exception = last_exception
        self.attempts = attempts

    def __str__(self) -> str:
        base = f"Retry exhausted after {self.attempts} attempts: {self.args[0]}"
        if self.last_exception:
            base += f" | last_exc={type(self.last_exception).__name__}: {self.last_exception}"
        return base


class CircuitBreakerOpenError(CurlxError):
    """Raised when the circuit breaker is open and requests are blocked."""

    pass


class ProxyError(CurlxError):
    """Raised when a proxy-related error occurs."""

    pass
