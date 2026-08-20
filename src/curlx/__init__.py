"""
curlx - Production-grade HTTP client SDK built on curl_cffi.

Provides async & sync clients with TLS fingerprint impersonation, per-request
proxy rotation, smart retry with a circuit breaker, token-bucket rate limiting
and persistent cookie jars.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from curlx.client import AsyncHttpClient, SyncHttpClient
from curlx.cookies import CookieJar
from curlx.exceptions import (
    CertificateError,
    CircuitBreakerOpenError,
    ConnectionFailedError,
    CurlxError,
    DNSResolutionError,
    HttpStatusError,
    IdentityExhaustedError,
    IdentityStaleError,
    ImpersonationError,
    ProfileNotFoundError,
    ProxyError,
    ProxyPoolExhaustedError,
    ResponseInvalidError,
    RetryableResponseError,
    RetryExhaustedError,
    TimeoutExceededError,
    TlsError,
    TooManyRedirectsError,
    TransportError,
)
from curlx.fingerprint import BrowserProfile, get_profile, list_profiles
from curlx.headers import HeaderBuilder
from curlx.identity import (
    BoundSession,
    Identity,
    IdentityPool,
    QuotaGate,
    SessionStats,
)
from curlx.middleware import MiddlewareChain
from curlx.models import RequestConfig, Response
from curlx.proxy import (
    EndpointProvider,
    Proxy,
    ProxyHealth,
    ProxyProvider,
    ProxyRotator,
    StaticProvider,
)
from curlx.ratelimit import RateLimiter
from curlx.retry import CircuitBreaker, RetryPolicy, is_retryable, with_retry
from curlx.session import SessionFactory

# Single source of truth is pyproject.toml; reading it back avoids the drift
# that had __init__ claiming 0.2.0 while the package metadata said 0.1.0.
try:
    __version__ = _pkg_version("curlx")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0.dev0"
__all__ = [
    # Clients
    "AsyncHttpClient",
    "SyncHttpClient",
    "SessionFactory",
    # Requests & responses
    "RequestConfig",
    "Response",
    # Proxying
    "Proxy",
    "ProxyHealth",
    "ProxyProvider",
    "ProxyRotator",
    "StaticProvider",
    "EndpointProvider",
    # Resilience
    "with_retry",
    "is_retryable",
    "CircuitBreaker",
    "RetryPolicy",
    "RateLimiter",
    # Bound identities
    "Identity",
    "BoundSession",
    "IdentityPool",
    "QuotaGate",
    "SessionStats",
    # Fingerprinting
    "BrowserProfile",
    "HeaderBuilder",
    "get_profile",
    "list_profiles",
    # Extensibility
    "MiddlewareChain",
    "CookieJar",
    # Exceptions
    "CurlxError",
    "HttpStatusError",
    "RetryExhaustedError",
    "CircuitBreakerOpenError",
    "ProfileNotFoundError",
    "TransportError",
    "ConnectionFailedError",
    "DNSResolutionError",
    "TimeoutExceededError",
    "TlsError",
    "CertificateError",
    "TooManyRedirectsError",
    "ImpersonationError",
    "ProxyError",
    "ProxyPoolExhaustedError",
    "ResponseInvalidError",
    "RetryableResponseError",
    "IdentityStaleError",
    "IdentityExhaustedError",
]
