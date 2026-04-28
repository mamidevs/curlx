"""
Advanced crawling example with:
- Proxy rotation
- Retry with exponential backoff
- Dynamic headers
- Circuit breaker
"""

import asyncio
from typing import Any, Dict, List

from curlx import AsyncHttpClient, CircuitBreaker, ProxyRotator, with_retry
from curlx.headers import HeaderBuilder


async def advanced_crawl():
    proxies = ProxyRotator(
        [
            "http://proxy1.example.com:8080",
            "http://proxy2.example.com:8080",
            # Add real proxies here
        ],
        strategy="round_robin",
    )

    headers = (
        HeaderBuilder()
        .with_browser("chrome")
        .with_accept_language("tr-TR,tr;q=0.9")
        .with_referer("https://www.google.com")
        .build()
    )

    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30)

    async with AsyncHttpClient(
        max_concurrent=50,
        impersonate="chrome137",
        proxies=proxies,
        timeout=20,
        default_headers=headers,
        circuit_breaker=cb,
    ) as client:

        @with_retry(max_attempts=3, min_wait=1, max_wait=10)
        async def fetch_item(item_id: int) -> Dict[str, Any]:
            url = f"https://api.example.com/items/{item_id}"
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

        item_ids = list(range(1, 101))
        tasks = [fetch_item(iid) for iid in item_ids]
        results: List[Any] = await asyncio.gather(*tasks, return_exceptions=True)

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]

        print(f"Success: {len(successes)} | Failed: {len(failures)}")
        if failures:
            print("First failure:", failures[0])


if __name__ == "__main__":
    asyncio.run(advanced_crawl())
