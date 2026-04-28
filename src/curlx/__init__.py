"""
curlx – Production-grade HTTP client SDK built on curl_cffi.

Provides async & sync clients with TLS fingerprint spoofing,
proxy rotation, smart retry, and rate limiting for stealth crawling.
"""

from curlx.client import AsyncHttpClient, SyncHttpClient
from curlx.proxy import ProxyRotator
from curlx.retry import with_retry, CircuitBreaker
from curlx.exceptions import (
    CurlxError,
    HttpStatusError,
    RetryExhaustedError,
    CircuitBreakerOpenError,
    ProxyError,
)
from curlx.models import Response
from curlx.headers import HeaderBuilder

__version__ = "0.1.0"
__all__ = [
    "AsyncHttpClient",
    "SyncHttpClient",
    "ProxyRotator",
    "with_retry",
    "CircuitBreaker",
    "CurlxError",
    "HttpStatusError",
    "RetryExhaustedError",
    "CircuitBreakerOpenError",
    "ProxyError",
    "Response",
    "HeaderBuilder",
]
