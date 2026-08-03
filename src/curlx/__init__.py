"""
curlx - Production-grade HTTP client SDK built on curl_cffi.

Provides async & sync clients with TLS fingerprint impersonation, per-request
proxy rotation, smart retry with a circuit breaker, token-bucket rate limiting
and persistent cookie jars.
"""

from curlx.client import AsyncHttpClient, SyncHttpClient
from curlx.cookies import CookieJar
from curlx.exceptions import (
    CertificateError,
    CircuitBreakerOpenError,
    ConnectionFailedError,
    CurlxError,
    DNSResolutionError,
    HttpStatusError,
    ImpersonationError,
    ProfileNotFoundError,
    ProxyError,
    ProxyPoolExhaustedError,
    RetryExhaustedError,
    TimeoutExceededError,
    TlsError,
    TooManyRedirectsError,
    TransportError,
)
from curlx.fingerprint import BrowserProfile, get_profile, list_profiles
from curlx.headers import HeaderBuilder
from curlx.middleware import MiddlewareChain
from curlx.models import RequestConfig, Response
from curlx.proxy import Proxy, ProxyRotator
from curlx.ratelimit import RateLimiter
from curlx.retry import CircuitBreaker, is_retryable, with_retry
from curlx.session import SessionFactory

__version__ = "0.2.0"
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
    "ProxyRotator",
    # Resilience
    "with_retry",
    "is_retryable",
    "CircuitBreaker",
    "RateLimiter",
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
]
