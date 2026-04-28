# curlx

Production-grade async/sync HTTP client SDK built on **curl_cffi** for stealth web crawling and bot-detection evasion.

## Features

- **TLS/JA3 Fingerprint Spoofing** – impersonate Chrome, Firefox, Safari, Edge
- **Async & Sync APIs** – unified interface for both paradigms
- **Smart Retry & Circuit Breaker** – exponential backoff, custom predicates
- **Proxy Rotation** – round-robin, random, sticky session strategies
- **User-Agent Rotation** – real browser UAs with per-request variation
- **Middleware / Interceptors** – hook into request/response lifecycle
- **Rate Limiting** – semaphore-based concurrency control
- **Cookie Persistence** – session-level cookie jar
- **Structured Logging** – detailed per-request logs
- **Type Safe** – fully typed with Pydantic models

## Quick Start

```bash
pip install curlx
```

### Async

```python
import asyncio
from curlx import AsyncHttpClient

async def main():
    async with AsyncHttpClient(
        impersonate="chrome137",
        max_concurrent=50,
        proxies="http://proxy.example.com:8080",
    ) as client:
        resp = await client.get("https://httpbin.org/json")
        print(resp.status_code)
        print(resp.json())

asyncio.run(main())
```

### Sync

```python
from curlx import SyncHttpClient

with SyncHttpClient(impersonate="firefox") as client:
    resp = client.get("https://httpbin.org/json")
    print(resp.status_code)
    print(resp.json())
```

### Advanced Crawl with Retry & Proxy Rotation

```python
from curlx import AsyncHttpClient, ProxyRotator, with_retry
from curlx.headers import HeaderBuilder

proxy_rotator = ProxyRotator([
    "http://user:pass@p1.example.com:8080",
    "http://user:pass@p2.example.com:8080",
], strategy="round_robin")

headers = HeaderBuilder()
    .with_browser("chrome")
    .with_referer("https://www.google.com")
    .build()

@with_retry(max_attempts=5, min_wait=1, max_wait=30)
async def fetch_all():
    async with AsyncHttpClient(
        max_concurrent=100,
        proxies=proxy_rotator,
        impersonate="chrome137",
        timeout=20,
    ) as client:
        tasks = [client.get(f"https://api.example.com/items/{i}") for i in range(1000)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        return responses

asyncio.run(fetch_all())
```

## Architecture

```
curlx/
├── client.py       # AsyncHttpClient, SyncHttpClient
├── session.py      # Session lifecycle & cookie jar
├── retry.py        # Retry decorator & circuit breaker
├── proxy.py        # Proxy rotation strategies
├── fingerprint.py  # Browser impersonation presets
├── headers.py      # Dynamic header builder
├── models.py       # Pydantic response models
├── exceptions.py   # Custom exception hierarchy
├── middleware.py   # Request/response interceptors
└── utils.py        # Helpers
```

## License

MIT
