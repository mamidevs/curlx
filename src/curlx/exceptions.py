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
# Semantic failures - a transport-level success the caller rejected
# ----------------------------------------------------------------------
class ResponseInvalidError(CurlxError):
    """
    A response that arrived intact but that the caller's ``validate`` rejected.

    ``status_code == 200`` is not a success criterion on most real targets: an
    empty body, a drifted session serving another tenant's data, or a WAF
    interstitial all arrive as a well-formed 200 and raise nothing. Only the
    caller can tell those apart, so ``validate`` raises one of these and the
    client turns it into a real failure.

    This base class is **terminal**: retrying it is pointless because the same
    request would be answered the same way. The subclasses below say what would
    have to change first.

    Args:
        message: Human-readable summary.
        url: Request URL, if known.
        response: The rejected response, kept so a handler can inspect the body
            without re-issuing the request.
        detail: Machine-ish discriminator the caller can branch on (``"empty
            body"``, ``"storeId mismatch"``); free-form by design, since only
            the caller knows its own taxonomy.
        blames_proxy: Whether the egress path, rather than the backend, is the
            likely cause. Drives proxy invalidation - see :meth:`Proxy.report`.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        response: object | None = None,
        detail: str = "",
        blames_proxy: bool = False,
    ) -> None:
        super().__init__(message, url=url)
        self.response = response
        self.detail = detail
        self.blames_proxy = blames_proxy

    def __str__(self) -> str:
        base = str(self.args[0])
        return f"{base} ({self.detail})" if self.detail else base


class RetryableResponseError(ResponseInvalidError):
    """
    A rejected response worth requesting again unchanged.

    Transient garbage: a truncated body, a momentarily inconsistent envelope, a
    cache serving a half-written page. The only entry of this family in
    :data:`curlx.retry.DEFAULT_RETRYABLE_EXCEPTIONS`.
    """


class IdentityStaleError(ResponseInvalidError):
    """
    The session's identity drifted and must be re-established before retrying.

    Classic shape: a cookie binding the session to a store, a region or a
    tenant silently lapses, and the backend keeps answering 200 - with somebody
    else's data. Repeating the request cannot help; something has to re-run the
    warm-up first.

    Deliberately **not** a :class:`RetryableResponseError`. ``with_retry`` has no
    way to warm a session up, so it would re-issue the identical request against
    the identical broken identity until the attempt budget ran out and then
    report exhaustion - a silent wrong answer dressed as a network problem.
    :class:`curlx.identity.BoundSession` is the actor that *can* repair this, and
    it catches this type by name.
    """


class IdentityExhaustedError(ResponseInvalidError):
    """
    The session's budget is spent; a fresh identity is needed to continue.

    Measured shape behind this: some quotas are counted per session rather than
    rated per second, so slowing down buys nothing and only a new identity
    resets the counter.

    Like :class:`IdentityStaleError`, this is not retryable by ``with_retry``
    and is handled by :class:`curlx.identity.BoundSession` instead.
    """


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
