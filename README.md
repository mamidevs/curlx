# curlx

Production-grade async/sync HTTP client SDK **and CLI** built on **curl_cffi** for stealth web crawling and bot-detection evasion.

## Features

- **TLS/JA3 Fingerprint Spoofing** – impersonate Chrome, Firefox, Safari, Edge, Tor
- **Async & Sync APIs** – unified interface for both paradigms, both with `fetch_all`
- **Smart Retry & Circuit Breaker** – exponential backoff with full jitter, `Retry-After` support, sliding-window breaker
- **Proxy Rotation** – round-robin, random and weighted strategies, rotated **per request**, with automatic invalidation of dead proxies
- **Rate Limiting** – token-bucket limiter, optionally per host
- **Concurrency Control** – separately configurable ceiling on in-flight requests
- **User-Agent Rotation** – real browser UAs, each paired with the profile it is coherent with
- **Middleware / Interceptors** – request, response and error hooks that actually fire
- **Cookie Persistence** – file-backed jar in JSON or Netscape format
- **Typed Exception Hierarchy** – curl_cffi errors translated into curlx types
- **SSRF-Safe Redirects** – redirects to private/internal IPs rejected by default
- **Beautiful CLI** – terminal HTTP client with syntax highlighting and colored output
- **Fully Type-Hinted** – annotated end to end and checked with `mypy --disallow-untyped-defs`; configuration objects are stdlib dataclasses, with no runtime-validation dependency

## Requirements

- Python **>= 3.12**
- `curl_cffi >= 0.16.0` — the floor the impersonation table requires, and the first release exposing the typed exception hierarchy curlx maps onto
- `typer >= 0.12.0`, `rich >= 13.0.0` — for the CLI

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
        impersonate="chrome",
        max_concurrent=50,
        proxies="http://proxy.example.com:8080",
    ) as client:
        resp = await client.get("https://httpbin.org/json")
        print(resp.status_code, resp.reason, resp.elapsed)
        print(resp.json())

asyncio.run(main())
```

### SDK – Sync

```python
from curlx import SyncHttpClient

with SyncHttpClient(impersonate="firefox") as client:
    resp = client.get("https://httpbin.org/json")
    print(resp.status_code)
    print(resp.json())
```

### Impersonation profiles

`impersonate` defaults to `"chrome"`, an alias curl_cffi resolves to its newest Chrome
target — so it moves forward automatically when curl_cffi is upgraded. Pin a version
when a fingerprint must stay byte-identical over time:

```python
from curlx import list_profiles, get_profile

get_profile("chrome")       # BrowserProfile(name='Chrome (latest)', alias_of='chrome146')
get_profile("chrome146")    # pinned; never moves
list_profiles(include_deprecated=False)   # every accepted profile name
```

### Fetching many URLs

Both clients expose `fetch_all`. The async one bounds in-flight requests with a
semaphore; the sync one fans out over a thread pool.

```python
urls = ["https://httpbin.org/get", "https://httpbin.org/ip"]

async with AsyncHttpClient(max_concurrent=20) as client:
    results = await client.fetch_all(urls)

# return_exceptions=True is the DEFAULT, so results holds a mix of
# Response objects and exceptions. Check before you use them.
for url, result in zip(urls, results, strict=True):
    if isinstance(result, BaseException):
        print(f"{url} failed: {type(result).__name__}: {result}")
    else:
        print(f"{url} -> {result.status_code}")
```

Pass `return_exceptions=False` to have the first failure propagate instead.

## Rate limiting vs. concurrency

These are two different controls and curlx keeps them separate:

| | What it bounds | Option |
|---|---|---|
| **Concurrency** | How many requests are *in flight at once* | `max_concurrent` |
| **Rate** | How many requests are *issued per second* | `rate_limiter` / `requests_per_second` |

A semaphore is a concurrency limiter. It says nothing about how fast requests are
issued — 50 requests that each finish in 10 ms are still 5 000 requests/second — and
issue rate is what servers measure when deciding to ban a client. `RateLimiter` is a
real token bucket:

```python
from curlx import AsyncHttpClient, RateLimiter

# 10 req/s sustained, bursts of up to 20 after an idle period,
# with an independent budget per host.
limiter = RateLimiter(10, burst=20, per_host=True)

async with AsyncHttpClient(rate_limiter=limiter, max_concurrent=50) as client:
    ...

# Shorthand when the defaults are fine (burst = ceil(rate), one shared bucket):
async with AsyncHttpClient(requests_per_second=10) as client:
    ...
```

`burst` defaults to `ceil(requests_per_second)`. Passing both `rate_limiter` and
`requests_per_second` raises `ValueError`. The limiter applies backpressure by
blocking — it never drops or rejects a request — and is usable from both threads and
coroutines (`acquire()` / `acquire_async()`), with all bucket state guarded by a single
`threading.Lock`.

## Cookie persistence

```python
from curlx import AsyncHttpClient, CookieJar

jar = CookieJar("~/.cache/curlx/session.json")          # JSON (default)
jar = CookieJar("cookies.txt", fmt="netscape")          # curl/wget format

async with AsyncHttpClient(cookie_jar=jar) as client:
    await client.get("https://example.com/login")
# cookies are written back atomically, at 0600, when the client closes
```

Cookies are loaded into the session on entry and persisted on exit. Reading never
raises: a missing, unreadable or corrupt jar yields `{}` and a warning, so a damaged
file degrades to a logged-out session instead of crashing the client at startup.

The `netscape` format is exact for *reading* a jar produced by curl. Writing one is
lossy — curlx models a cookie as a bare name/value pair, so the domain, path, secure
and expiry columns are filled with placeholders and curl will not send those cookies
to a real host. Prefer `json` for storage curlx owns.

## Middleware hooks

Hooks fire on every request, on both clients. Register them on a `MiddlewareChain`:

```python
from dataclasses import replace
from curlx import AsyncHttpClient, MiddlewareChain, RequestConfig, Response

def add_trace_header(config: RequestConfig) -> RequestConfig:
    # RequestConfig is frozen — derive a new one and RETURN it.
    return replace(config, headers={**config.headers, "X-Trace": "abc123"})

def log_response(resp: Response) -> Response:
    print(resp.status_code, resp.url, resp.elapsed)
    return resp

def log_error(exc: Exception, config: RequestConfig) -> None:
    print(f"{config.method} {config.url} failed: {type(exc).__name__}")

chain = (
    MiddlewareChain()
    .add_request_hook(add_trace_header)
    .add_response_hook(log_response)
    .add_error_hook(log_error)
)

async with AsyncHttpClient(middleware=chain) as client:
    await client.get("https://example.com")
```

Two rules the chain enforces rather than papering over:

- A request or response hook **must return** the (possibly modified) object it was
  handed. Returning `None` raises `CurlxError` instead of silently dropping the hook's
  effect. Error hooks legitimately return `None`.
- Hooks may be plain functions or coroutine functions. Handing an **async hook to
  `SyncHttpClient` raises `CurlxError`** — there is no event loop to await it in, and
  skipping it would drop its effect.

## Exception hierarchy

curl_cffi's exceptions are translated into curlx types, so nothing downstream has to
import from curl_cffi:

```
CurlxError
├── HttpStatusError          # .status_code, .response_body, .retry_after
├── RetryExhaustedError      # .last_exception, .attempts
├── CircuitBreakerOpenError
├── ProfileNotFoundError
└── TransportError
    ├── ConnectionFailedError
    │   └── DNSResolutionError
    ├── TimeoutExceededError
    ├── TlsError
    │   └── CertificateError
    ├── TooManyRedirectsError
    ├── ImpersonationError
    └── ProxyError
        └── ProxyPoolExhaustedError
```

```python
from curlx import CurlxError, HttpStatusError, TransportError

try:
    resp = await client.get(url)
except HttpStatusError as exc:          # only with raise_for_status=True
    print(exc.status_code, exc.retry_after)
except TransportError as exc:           # network, DNS, TLS, proxy, timeout
    print(f"{type(exc).__name__}: {exc}")
except CurlxError as exc:               # anything else curlx raises
    print(exc)
```

Every `CurlxError` carries `.url`. Catching `CurlxError` alone is enough to cover the
whole surface.

## Retry & circuit breaker

```python
from curlx import with_retry, is_retryable, CircuitBreaker

@with_retry(max_attempts=5, min_wait=1, max_wait=30)
async def fetch(url):
    return await client.get(url)
```

`with_retry` works on sync functions, async functions, `functools.partial` objects and
callable instances — the wrapper is chosen from what it is applied to. It retries the
`TransportError` subtree and the status codes `408, 409, 425, 429, 500, 502, 503, 504`,
using full jitter (`uniform(0, min(min_wait * 2**(attempt-1), max_wait))`) and honouring
a server's `Retry-After` header in preference to its own backoff.

**Non-retryable exceptions propagate unchanged.** A `ValueError` from your own code, or
a 404, comes out as itself — not rewrapped as `RetryExhaustedError` — so `except ValueError:`
around a decorated call still works. Only genuine exhaustion raises `RetryExhaustedError`.
Some errors are deliberately excluded even though their parent class is retryable:
`CertificateError` and `ProxyPoolExhaustedError` cannot succeed on a second attempt.

Use `is_retryable(exc)` to reuse the exact same predicate outside a decorated function.

```python
breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0, failure_window=60.0)

async with AsyncHttpClient(circuit_breaker=breaker, raise_for_status=True) as client:
    ...

breaker.state          # "CLOSED" | "OPEN" | "HALF_OPEN"; reading has no side effects
breaker.failure_count  # failures currently inside the rolling window
```

Failures are counted over a sliding window rather than as a strictly consecutive run, so
a backend failing half its requests still trips the breaker. The breaker wraps the send
itself, so **transport failures always count; 4xx/5xx responses only count when the client
was built with `raise_for_status=True`** — otherwise a bad status is a perfectly ordinary
return value it never sees.

## Proxy rotation

```python
from curlx import AsyncHttpClient, ProxyRotator, ProxyPoolExhaustedError

rotator = ProxyRotator(
    ["http://user:pass@p1.example.com:8080", "http://user:pass@p2.example.com:8080"],
    strategy="round_robin",     # or "random", "weighted_random"
    sticky_sessions=False,      # bind a session_key to one proxy when True
)

async with AsyncHttpClient(proxies=rotator) as client:
    ...   # a proxy is picked per request, not once per client
```

A request that fails at the transport level retires the proxy that carried it. When
every proxy has been invalidated the next acquire raises `ProxyPoolExhaustedError`
rather than falling back to a direct connection — that fallback would leak the real IP
of a caller who explicitly asked to go through a proxy. Call `rotator.restore()` to
bring the pool back.

`proxies` also accepts a plain URL string, or a per-scheme `{"http": ..., "https": ...}`
dict which is applied verbatim to every request (no rotation).

## Redirect safety

`allow_redirects` defaults to `"safe"`: redirects are followed, but **not** to private or
internal IP addresses. That closes the SSRF pivot a redirect-following crawler otherwise
offers to any server it visits (GHSA-qw2m-4pqf-rmpp).

```python
AsyncHttpClient()                          # "safe" — the default
AsyncHttpClient(allow_redirects=True)      # follow everything, including internal IPs
AsyncHttpClient(allow_redirects=False)     # never follow
AsyncHttpClient(max_redirects=5)           # cap the chain; TooManyRedirectsError beyond it
```

`Response.history` and `Response.redirect_count` describe the chain that was followed.

## Header order

Header *order* is itself a fingerprint: a real browser emits its headers in a fixed
sequence, and a mismatch is detectable even when every individual header is right.

```python
AsyncHttpClient(header_order="Host,Connection,User-Agent,Accept,Referer,Accept-Encoding")
```

curl_cffi already emits a correctly ordered block for the active `impersonate` target,
so set this only when overriding that ordering deliberately.

For header *contents*, `HeaderBuilder` stays coherent with the impersonation target:

```python
from curlx import HeaderBuilder

headers = (
    HeaderBuilder()
    .with_browser("chrome")
    .with_accept_language("tr-TR,tr;q=0.9")
    .with_referer("https://www.google.com")
    .build()
)
```

It emits **no** `User-Agent` and no `sec-ch-ua*` headers unless you set one explicitly,
because curl_cffi already supplies values matched to the impersonation target. Overriding
only the User-Agent is what makes a client trivially detectable — the TLS handshake says
one browser while the headers say another.

## Client options

All keyword-only, on both `AsyncHttpClient` and `SyncHttpClient`:

| Option | Default | Purpose |
|---|---|---|
| `max_concurrent` | `100` | In-flight request ceiling (async semaphore / sync pool size) |
| `impersonate` | `"chrome"` | Profile name or `BrowserProfile` |
| `proxies` | `None` | URL string, per-scheme dict, or `ProxyRotator` |
| `timeout` | `30.0` | Seconds |
| `verify` | `True` | TLS certificate verification |
| `max_clients` | `max_concurrent` | curl_cffi connection pool size |
| `default_headers` | `None` | Merged into every request |
| `middleware` | `None` | `MiddlewareChain` |
| `circuit_breaker` | `None` | `CircuitBreaker` |
| `rate_limiter` | `None` | `RateLimiter` (mutually exclusive with the next) |
| `requests_per_second` | `None` | Shorthand that builds a `RateLimiter` |
| `cookie_jar` | `None` | `CookieJar` |
| `raise_for_status` | `False` | Raise `HttpStatusError` on 4xx/5xx |
| `allow_redirects` | `"safe"` | `"safe"`, `True` or `False` |
| `max_redirects` | `None` | Redirect chain cap |
| `header_order` | `None` | Comma-separated header order |
| `doh_url` | `None` | DNS-over-HTTPS resolver |
| `interface` | `None` | Bind to a network interface |
| `cert` | `None` | Client certificate |
| `http_version` | `None` | Force an HTTP version |
| `session_key` | `None` | Sticky-session key for the proxy rotator |

`close()` / `aclose()` are public, so a client can be managed without a `with` block.

## Response

`Response` wraps the curl_cffi response and adds:

`status_code` · `reason` · `url` · `headers` · `cookies` · `content` · `text` ·
`encoding` · `json()` · `elapsed` (a `timedelta`) · `history` · `redirect_count` ·
`primary_ip` · `http_version` · `response_size` · `retry_after` ·
`raise_for_status()` · `is_success()` / `is_redirect()` / `is_client_error()` /
`is_server_error()` · `raw_response`

## CLI – Terminal HTTP Client

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
  --impersonate firefox \
  --verbose

# Save response to file
curlx get https://api.example.com/data -o data.json

# Non-zero exit on 4xx/5xx, for scripts
curlx get https://api.example.com/data --fail

# List supported browser profiles
curlx profiles

# Show sample User-Agent strings
curlx user-agents chrome
```

### CLI Reference

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

Options (every verb):
  -H, --header             Add a header (repeatable). 'Key: Value', or 'Key;' to send empty
      --proxy              Proxy URL, e.g. http://user:pass@host:8080
  -i, --impersonate        Browser fingerprint to impersonate (default: chrome)
  -t, --timeout            Request timeout in seconds (default: 30)
  -o, --output             Write response body to file
  -p, --pretty/-P, --no-pretty
                           Pretty-print JSON (interactive terminal only; pipes stay verbatim)
  -v, --verbose            Verbose output (headers, timing, etc.)
      --no-color           Disable colored output
  -k, --insecure           Disable SSL certificate verification
  -e, --referer            Referer header
  -A, --user-agent         Custom User-Agent string
  -f, --fail               Exit with code 4 when the response is 4xx or 5xx

Body options (post, put, patch, delete only):
  -j, --json               JSON body string
  -d, --data               Raw body string
```

A repeated `-H` key is **last-wins** and warns on stderr — the client takes a dict, so
duplicates cannot be preserved.

Unlike curl's `--fail`, curlx's `-f` only changes the **exit code**: the body is still
printed and `-o` still writes the file.

### Output streams

The response body goes to **stdout**; every diagnostic — status summary, header tables,
warnings, errors — goes to **stderr**. When stdout is not a TTY the body is passed
through verbatim, with no panels, colors or 80-column wrapping, so these work as written:

```bash
curlx get https://api.example.com/data | jq .
curlx get https://api.example.com/data > body.json
```

Exit codes: `0` success · `1` usage/validation error · `2` command-line parsing error ·
`3` network or transport failure · `4` HTTP 4xx/5xx with `--fail` · `5` could not write
`--output` file · `6` unexpected internal error.

## Advanced Crawl with SDK

See [`examples/advanced_crawl.py`](examples/advanced_crawl.py) for the full version —
proxy rotation, rate limiting, a circuit breaker, a cookie jar, middleware and retry
wired together.

```python
import asyncio
from curlx import (
    AsyncHttpClient, CircuitBreaker, CookieJar,
    ProxyRotator, RateLimiter, with_retry,
)

async def main():
    proxies = ProxyRotator([
        "http://user:pass@p1.example.com:8080",
        "http://user:pass@p2.example.com:8080",
    ], strategy="round_robin")

    async with AsyncHttpClient(
        max_concurrent=100,
        impersonate="chrome",
        proxies=proxies,
        rate_limiter=RateLimiter(10, burst=20, per_host=True),
        circuit_breaker=CircuitBreaker(failure_threshold=5),
        cookie_jar=CookieJar("~/.cache/curlx/session.json"),
        raise_for_status=True,
        timeout=20,
    ) as client:

        @with_retry(max_attempts=5, min_wait=1, max_wait=30)
        async def fetch(item_id: int):
            resp = await client.get(f"https://api.example.com/items/{item_id}")
            return resp.json()

        results = await asyncio.gather(
            *(fetch(i) for i in range(1000)), return_exceptions=True
        )
        return results

asyncio.run(main())
```

## Examples

- [`examples/basic_usage.py`](examples/basic_usage.py) – sync, async, `fetch_all` on both clients, error handling
- [`examples/advanced_crawl.py`](examples/advanced_crawl.py) – the full resilience stack

## Architecture

```
curlx/
├── client.py       # AsyncHttpClient, SyncHttpClient — the request pipeline
├── session.py      # curl_cffi session lifecycle & configuration
├── retry.py        # Retry decorator, retry predicate & circuit breaker
├── ratelimit.py    # Token-bucket RateLimiter (sync + async)
├── cookies.py      # CookieJar — persistent cookie storage
├── proxy.py        # Proxy rotation strategies & invalidation
├── fingerprint.py  # Browser impersonation profile table
├── headers.py      # Dynamic header builder & User-Agent pools
├── models.py       # RequestConfig dataclass & Response wrapper
├── exceptions.py   # Exception hierarchy & curl_cffi error mapping
├── middleware.py   # Request/response/error interceptors
├── cli.py          # Typer CLI application
├── cli_output.py   # Rich terminal output formatter
└── utils.py        # Helpers
```

Every request funnels through one pipeline:

```
rate limit -> middleware.request -> circuit breaker -> transport
                                                    -> middleware.response
                   middleware.error / proxy invalidation on failure
```

## License

MIT
