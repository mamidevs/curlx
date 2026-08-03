"""
Advanced crawling example - the flagship demo.

Wires together every resilience feature curlx advertises:

- per-request proxy rotation, with automatic invalidation of dead proxies
- a token-bucket rate limiter (requests per second, per host)
- a circuit breaker that stops hammering a backend that is already failing
- a persistent cookie jar
- a request middleware hook
- retry with exponential backoff and full jitter

The URLs are placeholders. Point them at a real target - and put real proxies in
the rotator - before running this.
"""

import asyncio
import itertools
from dataclasses import replace
from typing import Any

from curlx import (
    AsyncHttpClient,
    CircuitBreaker,
    CookieJar,
    HeaderBuilder,
    MiddlewareChain,
    ProxyRotator,
    RateLimiter,
    RequestConfig,
    with_retry,
)


def build_middleware() -> MiddlewareChain:
    """Stamp an incrementing correlation id on every outgoing request."""
    counter = itertools.count(1)

    def tag_request(config: RequestConfig) -> RequestConfig:
        # RequestConfig is frozen, so a hook derives a new one with
        # dataclasses.replace and *must* return it. A hook that returns None
        # raises CurlxError rather than silently dropping its own effect.
        return replace(
            config,
            headers={**config.headers, "X-Crawl-Id": str(next(counter))},
        )

    return MiddlewareChain().add_request_hook(tag_request)


async def advanced_crawl() -> None:
    # Resolved per request, not once per client: these 100 requests spread
    # across both proxies. A transport failure retires the proxy that caused it,
    # and once the pool is empty acquire() raises ProxyPoolExhaustedError rather
    # than quietly falling back to a direct connection that would leak the
    # real IP of a caller who explicitly asked for a proxy.
    proxies = ProxyRotator(
        [
            "http://user:pass@proxy1.example.com:8080",
            "http://user:pass@proxy2.example.com:8080",
        ],
        strategy="round_robin",
    )

    # No User-Agent is set on purpose: curl_cffi already emits one matching the
    # impersonation target. Overriding only the UA is what makes a client
    # trivially detectable - the TLS handshake would say one browser while the
    # request headers say another.
    headers = (
        HeaderBuilder()
        .with_browser("chrome")
        .with_accept_language("tr-TR,tr;q=0.9")
        .with_referer("https://www.google.com")
        .build()
    )

    # Trips to OPEN after 5 failures within a 60s sliding window, then rejects
    # every request with CircuitBreakerOpenError until recovery_timeout has
    # passed and a single probe is admitted.
    #
    # Where it trips: the breaker wraps the send itself, and raise_for_status=True
    # below makes a 4xx/5xx raise *inside* that wrapped call - so status errors
    # count as failures alongside transport errors. Leave raise_for_status at its
    # default of False and only transport failures ever reach the breaker.
    breaker = CircuitBreaker(
        failure_threshold=5,
        recovery_timeout=30.0,
        failure_window=60.0,
    )

    jar = CookieJar("~/.cache/curlx/crawl-session.json")

    async with AsyncHttpClient(
        max_concurrent=50,
        impersonate="chrome",
        proxies=proxies,
        timeout=20,
        default_headers=headers,
        middleware=build_middleware(),
        circuit_breaker=breaker,
        # 10 req/s sustained per host, absorbing bursts of up to 20. Passing
        # requests_per_second=10 builds an equivalent limiter with default
        # burst and no per-host split; supplying both raises ValueError.
        rate_limiter=RateLimiter(10, burst=20, per_host=True),
        # Loaded into the session on entry, written back atomically on exit.
        cookie_jar=jar,
        raise_for_status=True,
        # The default. Redirects to private/internal IPs are rejected, which
        # closes the SSRF pivot a redirect-following crawler otherwise offers.
        # Pass allow_redirects=True to opt out.
        allow_redirects="safe",
    ) as client:

        @with_retry(max_attempts=3, min_wait=1, max_wait=10)
        async def fetch_item(item_id: int) -> Any:
            # Retries the TransportError subtree and the retryable status codes
            # (408/409/425/429, 500/502/503/504), preferring the server's
            # Retry-After over the computed backoff when one is sent.
            #
            # Everything else propagates unchanged rather than being rewrapped:
            # a 404 arrives as HttpStatusError, a CircuitBreakerOpenError as
            # itself. Only genuine exhaustion raises RetryExhaustedError.
            resp = await client.get(f"https://api.example.com/items/{item_id}")
            return resp.json()

        results = await asyncio.gather(
            *(fetch_item(item_id) for item_id in range(1, 101)),
            return_exceptions=True,
        )

    # BaseException, not Exception: gather can hand back a CancelledError too,
    # and that does not derive from Exception.
    failures = [r for r in results if isinstance(r, BaseException)]
    successes = [r for r in results if not isinstance(r, BaseException)]

    print(f"Success: {len(successes)} | Failed: {len(failures)}")
    print(f"Breaker: {breaker.state} | failures in window: {breaker.failure_count}")
    print(f"Proxies still in pool: {proxies.count}")
    print(f"Cookies persisted: {len(jar)} -> {jar.path}")

    if failures:
        by_type: dict[str, int] = {}
        for exc in failures:
            name = type(exc).__name__
            by_type[name] = by_type.get(name, 0) + 1
        print("Failure breakdown:", by_type)


if __name__ == "__main__":
    asyncio.run(advanced_crawl())
