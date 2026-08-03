"""
Basic usage examples for curlx.

Every request here goes to httpbin.org and needs no proxy or credentials.
"""

import asyncio

from curlx import (
    AsyncHttpClient,
    CurlxError,
    HttpStatusError,
    Response,
    SyncHttpClient,
)

URLS = [
    "https://httpbin.org/get",
    "https://httpbin.org/ip",
    "https://httpbin.org/user-agent",
]


def report(url: str, result: Response | BaseException) -> None:
    """
    Print one ``fetch_all`` result.

    ``fetch_all`` defaults to ``return_exceptions=True``, so the list it returns
    holds a mix of :class:`Response` objects and exceptions. Printing an element
    without this check is how a crawl ends up reporting failures as successes.
    """
    if isinstance(result, BaseException):
        print(f"{url} -> FAILED {type(result).__name__}: {result}")
    else:
        print(f"{url} -> {result.status_code} {result.reason} in {result.elapsed}")


def sync_example() -> None:
    # "chrome" is the default profile: an alias curl_cffi resolves to its newest
    # Chrome target, so it moves forward automatically on a curl_cffi upgrade.
    # Pin a version ("chrome146") instead when the fingerprint must stay stable.
    with SyncHttpClient(impersonate="chrome", timeout=10) as client:
        resp = client.get("https://httpbin.org/get")
        print("Status:", resp.status_code, resp.reason)
        print("Elapsed:", resp.elapsed)
        print("JSON:", resp.json())


async def async_example() -> None:
    async with AsyncHttpClient(
        impersonate="chrome",
        max_concurrent=20,
        timeout=10,
    ) as client:
        resp = await client.get("https://httpbin.org/get")
        print("Status:", resp.status_code, resp.reason)
        print("Elapsed:", resp.elapsed)
        print("JSON:", resp.json())


async def async_concurrent_fetch() -> None:
    """Fan out with the async client; ``max_concurrent`` bounds requests in flight."""
    async with AsyncHttpClient(max_concurrent=5, timeout=10) as client:
        results = await client.fetch_all(URLS)
        for url, result in zip(URLS, results, strict=True):
            report(url, result)


def sync_concurrent_fetch() -> None:
    """
    The sync client fans out too, over a thread pool.

    ``max_concurrent`` caps the worker count. The proxy rotator and rate limiter
    are lock-guarded, so sharing one client across those threads is safe.
    """
    with SyncHttpClient(max_concurrent=3, timeout=10) as client:
        results = client.fetch_all(URLS)
        for url, result in zip(URLS, results, strict=True):
            report(url, result)


def error_handling() -> None:
    """
    curl_cffi's exceptions are translated into the curlx tree.

    One ``except CurlxError`` therefore covers transport, TLS, proxy and HTTP
    status failures without importing anything from curl_cffi.
    """
    # raise_for_status=True turns a 4xx/5xx response into an HttpStatusError
    # instead of returning it.
    with SyncHttpClient(timeout=10, raise_for_status=True) as client:
        try:
            client.get("https://httpbin.org/status/503")
        except HttpStatusError as exc:
            print(f"HTTP {exc.status_code} | retry_after={exc.retry_after}")
        except CurlxError as exc:
            print(f"Request failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    print("=== Sync ===")
    sync_example()

    print("\n=== Async ===")
    asyncio.run(async_example())

    print("\n=== Async fetch_all ===")
    asyncio.run(async_concurrent_fetch())

    print("\n=== Sync fetch_all ===")
    sync_concurrent_fetch()

    print("\n=== Error handling ===")
    error_handling()
