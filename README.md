# curlx

Production-grade async/sync HTTP client SDK **and CLI** built on **curl_cffi** for stealth web crawling and bot-detection evasion.

## Features

- **TLS/JA3 Fingerprint Spoofing** – impersonate Chrome, Firefox, Safari, Edge, Tor
- **Async & Sync APIs** – unified interface for both paradigms
- **Smart Retry & Circuit Breaker** – exponential backoff, custom predicates
- **Proxy Rotation** – round-robin, random, weighted, sticky session strategies
- **User-Agent Rotation** – real browser UAs with per-request variation
- **Middleware / Interceptors** – hook into request/response lifecycle
- **Rate Limiting** – semaphore-based concurrency control
- **Cookie Persistence** – session-level cookie jar
- **Structured Logging** – detailed per-request logs
- **Beautiful CLI** – terminal HTTP client with syntax highlighting & colored output
- **Type Safe** – fully typed with Pydantic models

## Installation

```bash
pip install curlx
```

## Quick Start

### SDK – Async

```python
import asyncio
from curlx import AsyncHttpClient

async def main():
    async with AsyncHttpClient(
        impersonate="chrome136",
        max_concurrent=50,
        proxies="http://proxy.example.com:8080",
    ) as client:
        resp = await client.get("https://httpbin.org/json")
        print(resp.status_code)
        print(resp.json())

asyncio.run(main())
```

### SDK – Sync

```python
from curlx import SyncHttpClient

with SyncHttpClient(impersonate="firefox133") as client:
    resp = client.get("https://httpbin.org/json")
    print(resp.status_code)
    print(resp.json())
```

### CLI – Terminal HTTP Client

```bash
# Simple GET
curlx get https://httpbin.org/get

# POST with JSON body
curlx post https://httpbin.org/post \
  -H "Content-Type: application/json" \
  --json '{"name":"foo"}'

# With proxy, custom fingerprint and verbose output
curlx get https://httpbin.org/get \
  --proxy http://proxy:8080 \
  --impersonate firefox133 \
  --verbose

# Save response to file
curlx get https://api.example.com/data -o data.json

# List supported browser profiles
curlx profiles

# Show sample User-Agent strings
curlx user-agents chrome
```

### Advanced Crawl with SDK

```python
from curlx import AsyncHttpClient, ProxyRotator, with_retry
from curlx.headers import HeaderBuilder

proxy_rotator = ProxyRotator([
    "http://user:pass@p1.example.com:8080",
    "http://user:pass@p2.example.com:8080",
], strategy="round_robin")

headers = (
    HeaderBuilder()
    .with_browser("chrome")
    .with_referer("https://www.google.com")
    .build()
)

@with_retry(max_attempts=5, min_wait=1, max_wait=30)
async def fetch_all():
    async with AsyncHttpClient(
        max_concurrent=100,
        proxies=proxy_rotator,
        impersonate="chrome136",
        timeout=20,
    ) as client:
        tasks = [client.get(f"https://api.example.com/items/{i}") for i in range(1000)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        return responses

asyncio.run(fetch_all())
```

## CLI Reference

```
curlx [COMMAND] [OPTIONS] URL

Commands:
  get      Send a GET request
  post     Send a POST request
  put      Send a PUT request
  patch    Send a PATCH request
  delete   Send a DELETE request
  head     Send a HEAD request
  profiles List supported browser profiles
  user-agents  Show sample User-Agent strings

Options:
  -H, --header        Add header (repeatable)
  --proxy             Proxy URL
  -i, --impersonate   Browser fingerprint (default: chrome136)
  -t, --timeout       Timeout in seconds (default: 30)
  -j, --json          JSON body
  -d, --data          Raw body
  -o, --output        Write body to file
  -p, --pretty        Pretty-print JSON (default: True)
  -v, --verbose       Verbose output
  --no-color          Disable colors
  -k, --insecure      Disable SSL verification
  -e, --referer       Referer header
  -A, --user-agent    Custom User-Agent
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
├── cli.py          # Typer CLI application
├── cli_output.py   # Rich terminal output formatter
└── utils.py        # Helpers
```

## License

MIT
