"""
Custom exception hierarchy for curlx.

Transport failures raised by curl_cffi are translated into the ``TransportError``
subtree by :func:`map_transport_error`, so callers can discriminate on a stable
curlx type instead of importing curl_cffi's own exceptions.
"""

from __future__ import annotations


class CurlxError(Exception):
    """Base exception for all curlx errors."""

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class HttpStatusError(CurlxError):
    """Raised when an HTTP response has an unexpected status code."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        url: str | None = None,
        response_body: bytes | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, url=url)
        self.status_code = status_code
        self.response_body = response_body
        # Seconds the server asked us to wait, parsed from the Retry-After header.
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"HTTP {self.status_code}: {self.args[0]} (url={self.url})"


class RetryExhaustedError(CurlxError):
    """Raised when all retry attempts have been exhausted."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        last_exception: BaseException | None = None,
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


class ProfileNotFoundError(CurlxError):
    """Raised when an unknown impersonation profile is requested."""


# ----------------------------------------------------------------------
# Transport errors - mapped from curl_cffi.requests.exceptions
# ----------------------------------------------------------------------
class TransportError(CurlxError):
    """Base class for network-level failures surfaced by curl_cffi."""


class ConnectionFailedError(TransportError):
    """The connection to the server could not be established."""


class DNSResolutionError(ConnectionFailedError):
    """The hostname could not be resolved."""


class TimeoutExceededError(TransportError):
    """The request exceeded its timeout."""


class TlsError(TransportError):
    """A TLS/SSL negotiation error occurred."""


class CertificateError(TlsError):
    """Certificate verification failed."""


class TooManyRedirectsError(TransportError):
    """The redirect limit was exceeded."""


class ImpersonationError(TransportError):
    """curl_cffi could not apply the requested impersonation profile."""


class ProxyError(TransportError):
    """Raised when a proxy-related error occurs."""


class ProxyPoolExhaustedError(ProxyError):
    """
    Every proxy in the rotator has been invalidated.

    Raised instead of silently falling back to a direct connection, which would
    leak the real IP of a client that explicitly asked to go through a proxy.
    """


def map_transport_error(exc: BaseException, *, url: str | None = None) -> CurlxError:
    """
    Translate a curl_cffi exception into the curlx ``TransportError`` subtree.

    Anything that is already a :class:`CurlxError` passes through untouched, and
    anything unrecognized becomes a generic :class:`TransportError`, so callers
    never have to catch curl_cffi types directly.
    """
    if isinstance(exc, CurlxError):
        return exc

    # Imported lazily so this module stays importable without curl_cffi present.
    from curl_cffi.requests import exceptions as ccex

    # Ordered most-specific first: DNSError subclasses ConnectionError, and
    # CertificateVerifyError subclasses SSLError, which subclasses ConnectionError.
    mapping: tuple[tuple[type[BaseException], type[CurlxError]], ...] = (
        (ccex.CertificateVerifyError, CertificateError),
        (ccex.SSLError, TlsError),
        (ccex.DNSError, DNSResolutionError),
        (ccex.ProxyError, ProxyError),
        (ccex.Timeout, TimeoutExceededError),
        (ccex.TooManyRedirects, TooManyRedirectsError),
        (ccex.ImpersonateError, ImpersonationError),
        (ccex.ConnectionError, ConnectionFailedError),
    )
    for curl_type, curlx_type in mapping:
        if isinstance(exc, curl_type):
            return curlx_type(str(exc), url=url)

    if isinstance(exc, ccex.RequestException):
        return TransportError(str(exc), url=url)

    # Builtins that curl_cffi may let through unwrapped.
    if isinstance(exc, TimeoutError):
        return TimeoutExceededError(str(exc), url=url)
    if isinstance(exc, ConnectionError):
        return ConnectionFailedError(str(exc), url=url)

    return TransportError(str(exc), url=url)
