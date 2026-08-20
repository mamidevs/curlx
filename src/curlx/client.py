"""
AsyncHttpClient & SyncHttpClient - the main entry points.

Both clients funnel every request through one pipeline:

    rate limit -> middleware.request -> circuit breaker -> transport
                                                        -> middleware.response
                       middleware.error / proxy invalidation on failure

Everything except the ``await`` keywords is shared between the two.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, cast

from curl_cffi import requests as curl_requests

from curlx.cookies import CookieJar
from curlx.exceptions import (
    ProxyError,
    ResponseInvalidError,
    TransportError,
    map_transport_error,
)
from curlx.fingerprint import BrowserProfile, get_profile
from curlx.middleware import MiddlewareChain
from curlx.models import RequestConfig, Response
from curlx.proxy import Proxy, ProxyRotator
from curlx.ratelimit import RateLimiter
from curlx.retry import CircuitBreaker
from curlx.session import SAFE_REDIRECTS, SessionFactory
from curlx.utils import get_logger, merge_dicts

if TYPE_CHECKING:
    from curl_cffi.requests import HttpMethod

logger = get_logger("curlx.client")

# Request kwargs curlx shapes explicitly; anything else rides along in
# RequestConfig.extra and is forwarded to curl_cffi untouched.
_BODY_KEYS = ("params", "json", "data", "cookies")


class _BaseClient:
    """Shared logic between sync and async clients."""

    def __init__(
        self,
        *,
        max_concurrent: int = 100,
        impersonate: str | BrowserProfile = "chrome",
        proxies: str | dict[str, str] | ProxyRotator | None = None,
        timeout: float = 30.0,
        verify: bool = True,
        max_clients: int | None = None,
        default_headers: dict[str, str] | None = None,
        middleware: MiddlewareChain | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        rate_limiter: RateLimiter | None = None,
        requests_per_second: float | None = None,
        cookie_jar: CookieJar | None = None,
        raise_for_status: bool = False,
        allow_redirects: Any = SAFE_REDIRECTS,
        max_redirects: int | None = None,
        header_order: str | None = None,
        doh_url: str | None = None,
        interface: str | None = None,
        cert: Any | None = None,
        http_version: Any | None = None,
        session_key: str | None = None,
        validate: Callable[[Response], None] | None = None,
    ) -> None:
        self.max_concurrent = max(max_concurrent, 1)
        self.max_clients = max_clients or self.max_concurrent
        self.timeout = timeout
        self.verify = verify
        self.default_headers = default_headers or {}
        self.middleware = middleware or MiddlewareChain()
        self.circuit_breaker = circuit_breaker
        self.raise_for_status = raise_for_status
        self.allow_redirects = allow_redirects
        self.cookie_jar = cookie_jar
        self.session_key = session_key
        # Runs on every response that reached the caller intact. Raising from it
        # is the only way to call a structurally-fine 200 a failure - see
        # ResponseInvalidError. None keeps the pre-validate behaviour exactly.
        self.validate = validate

        if rate_limiter is not None and requests_per_second is not None:
            raise ValueError("Pass either rate_limiter or requests_per_second, not both")
        if rate_limiter is not None:
            self.rate_limiter: RateLimiter | None = rate_limiter
        elif requests_per_second is not None:
            self.rate_limiter = RateLimiter(requests_per_second)
        else:
            self.rate_limiter = None

        self.profile = get_profile(impersonate) if isinstance(impersonate, str) else impersonate

        self.proxy_rotator, self._static_proxies = self._normalize_proxies(proxies)

        self._session_factory = SessionFactory(
            impersonate=self.profile,
            timeout=timeout,
            verify=verify,
            max_clients=self.max_clients,
            default_headers=self.default_headers,
            allow_redirects=allow_redirects,
            max_redirects=max_redirects,
            header_order=header_order,
            doh_url=doh_url,
            interface=interface,
            cert=cert,
            http_version=http_version,
        )

    # ------------------------------------------------------------------
    # Proxy handling
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_proxies(
        proxies: str | dict[str, str] | ProxyRotator | None,
    ) -> tuple[ProxyRotator | None, dict[str, str] | None]:
        if proxies is None:
            return None, None
        if isinstance(proxies, ProxyRotator):
            return proxies, None
        if isinstance(proxies, str):
            return ProxyRotator([proxies]), None
        if isinstance(proxies, dict):
            # A per-scheme dict cannot be expressed as one rotating proxy, so it
            # is kept verbatim and applied to every request.
            return None, dict(proxies)
        raise ProxyError(f"Unsupported proxies type: {type(proxies)!r}")

    def _resolve_proxies(
        self, session_key: str | None = None, host: str | None = None
    ) -> tuple[dict[str, str] | None, str | None]:
        """
        Return ``(proxy_dict, invalidatable_url)`` for the next request.

        The second element is the rotator entry to retire if the request fails;
        it is ``None`` for a static per-scheme dict.

        ``host`` lets the rotator skip proxies benched for this target in
        particular, which is a different thing from being broken outright.
        """
        if self._static_proxies is not None:
            return self._static_proxies, None
        if self.proxy_rotator is not None:
            proxy: Proxy = self.proxy_rotator.acquire(
                session_key=session_key or self.session_key, host=host
            )
            return proxy.to_curl_cffi(), proxy.url
        return None, None

    def _handle_transport_failure(self, exc: BaseException, proxy_url: str | None) -> None:
        """
        Retire the proxy that just failed, so the pool self-heals.

        Two things count as the proxy's fault. A ``TransportError`` is the
        obvious one - the hop never completed. The other is a
        ``ResponseInvalidError`` the caller explicitly blamed on the egress
        path: a block page or an interstitial arrives as a perfectly healthy
        200 over a perfectly healthy connection, so nothing at this layer can
        recognise it. Only the caller's ``validate`` knows, which is why the
        blame travels on the exception rather than being inferred here.
        """
        if proxy_url is None or self.proxy_rotator is None:
            return
        if isinstance(exc, TransportError) or (
            isinstance(exc, ResponseInvalidError) and exc.blames_proxy
        ):
            self.proxy_rotator.invalidate(proxy_url)

    # ------------------------------------------------------------------
    # Request shaping
    # ------------------------------------------------------------------
    def _build_config(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> RequestConfig:
        merged_headers = merge_dicts(self.default_headers, dict(headers) if headers else None)
        extra = {k: v for k, v in kwargs.items() if k not in _BODY_KEYS}
        return RequestConfig(
            method=method.upper(),
            url=url,
            headers=merged_headers,
            params=kwargs.get("params") or {},
            json_data=kwargs.get("json"),
            data=kwargs.get("data"),
            cookies=kwargs.get("cookies") or {},
            timeout=extra.pop("timeout", self.timeout),
            allow_redirects=extra.pop("allow_redirects", self.allow_redirects),
            verify=extra.pop("verify", self.verify),
            extra=extra,
        )

    @staticmethod
    def _config_to_kwargs(config: RequestConfig) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headers": dict(config.headers) or None,
            "timeout": config.timeout,
            "verify": config.verify,
            "allow_redirects": config.allow_redirects,
        }
        if config.params:
            kwargs["params"] = dict(config.params)
        if config.json_data is not None:
            kwargs["json"] = config.json_data
        if config.data is not None:
            kwargs["data"] = config.data
        if config.cookies:
            kwargs["cookies"] = dict(config.cookies)
        kwargs.update(config.extra)
        return kwargs

    def _finalize(self, raw: curl_requests.Response) -> Response:
        resp = Response(raw)
        if self.raise_for_status:
            resp.raise_for_status()
        return resp

    @staticmethod
    def _host_of(url: str) -> str | None:
        from urllib.parse import urlparse

        try:
            return urlparse(url).hostname
        except ValueError:
            return None

    def _load_cookies(self) -> dict[str, str] | None:
        if self.cookie_jar is None:
            return None
        cookies = self.cookie_jar.load()
        if cookies:
            logger.debug("Loaded %d cookie(s) from jar", len(cookies))
        return cookies or None

    def _persist_cookies(self, session: Any) -> None:
        if self.cookie_jar is None or session is None:
            return
        try:
            self.cookie_jar.save(dict(session.cookies))
        except Exception as exc:  # never let cookie persistence break teardown
            logger.warning("Could not persist cookies: %s", exc)


class AsyncHttpClient(_BaseClient):
    """Async HTTP client with semaphore-based concurrency."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._session: curl_requests.AsyncSession | None = None

    async def __aenter__(self) -> AsyncHttpClient:
        if self._session is not None:
            raise RuntimeError("Client is already entered; re-entering would leak the session.")
        self._session = self._session_factory.create_async_session()
        cookies = self._load_cookies()
        if cookies:
            self._session.cookies.update(cookies)
        logger.info(
            "AsyncHttpClient started | max_concurrent=%d impersonate=%s",
            self.max_concurrent,
            self.profile.name,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """
        Close the underlying session; safe to call more than once.

        Cookies are persisted on every close, including one triggered by a
        crash, so a partially finished crawl keeps its session. Teardown errors
        are logged rather than raised: `__aexit__` must not replace whatever
        exception the caller was already unwinding.
        """
        session, self._session = self._session, None
        if session is None:
            return
        try:
            self._persist_cookies(session)
            await session.close()
        except Exception as exc:
            logger.warning("Error closing AsyncHttpClient session: %s", exc)
        finally:
            logger.info("AsyncHttpClient closed")

    @property
    def session(self) -> curl_requests.AsyncSession:
        if self._session is None:
            raise RuntimeError("Client not entered. Use 'async with' context manager.")
        return self._session

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        # Popped before _build_config: an unrecognised kwarg lands in
        # RequestConfig.extra and is forwarded to curl_cffi verbatim, which
        # would fail on an argument it has never heard of.
        session_key: str | None = kwargs.pop("session_key", None)
        config = self._build_config(method, url, **kwargs)
        config = await self.middleware.process_request(config)
        logger.debug("%s %s", config.method, config.url)

        proxies, proxy_url = self._resolve_proxies(session_key, self._host_of(config.url))
        request_kwargs = self._config_to_kwargs(config)
        if proxies is not None:
            request_kwargs["proxies"] = proxies

        async def _send() -> Response:
            if self.rate_limiter is not None:
                await self.rate_limiter.acquire_async(self._host_of(config.url))
            async with self._semaphore:
                raw = await self.session.request(
                    cast("HttpMethod", config.method), config.url, **request_kwargs
                )
            return self._finalize(raw)

        try:
            if self.circuit_breaker is not None:
                resp = await self.circuit_breaker.call_async(_send)
            else:
                resp = await _send()
        # Inside the try, but *outside* the breaker. A backend that lies is not
        # a backend that is down: letting rejections trip the breaker deadlocks
        # recovery, because the warm-up request that would fix a stale identity
        # is itself refused by the open breaker. Being inside the try is what
        # matters - error hooks fire and a proxy-blaming rejection retires the
        # proxy, exactly as a transport failure would.
            if self.validate is not None:
                self.validate(resp)
        except BaseException as exc:
            mapped = map_transport_error(exc, url=config.url)
            self._handle_transport_failure(mapped, proxy_url)
            await self.middleware.process_error(mapped, config)
            raise mapped from exc

        logger.debug("%s %s -> %d", config.method, config.url, resp.status_code)
        return await self.middleware.process_response(resp)

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Response:
        return await self.request("HEAD", url, **kwargs)

    async def fetch_all(
        self,
        urls: Sequence[str],
        *,
        method: str = "GET",
        return_exceptions: bool = True,
        **kwargs: Any,
    ) -> list[Response | BaseException]:
        """
        Fire many requests concurrently and gather results.

        The semaphore still applies, so only ``max_concurrent`` run at once.
        With ``return_exceptions=True`` (the default) failures arrive as
        exception objects in the result list - check with ``isinstance``.
        """
        tasks = [self.request(method, url, **kwargs) for url in urls]
        return await asyncio.gather(*tasks, return_exceptions=return_exceptions)


class SyncHttpClient(_BaseClient):
    """Synchronous HTTP client."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session: curl_requests.Session | None = None

    def __enter__(self) -> SyncHttpClient:
        if self._session is not None:
            raise RuntimeError("Client is already entered; re-entering would leak the session.")
        self._session = self._session_factory.create_sync_session()
        cookies = self._load_cookies()
        if cookies:
            self._session.cookies.update(cookies)
        logger.info("SyncHttpClient started | impersonate=%s", self.profile.name)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """
        Close the underlying session; safe to call more than once.

        Cookies are persisted on every close, including one triggered by a
        crash, so a partially finished crawl keeps its session. Teardown errors
        are logged rather than raised: `__exit__` must not replace whatever
        exception the caller was already unwinding.
        """
        session, self._session = self._session, None
        if session is None:
            return
        try:
            self._persist_cookies(session)
            session.close()
        except Exception as exc:
            logger.warning("Error closing SyncHttpClient session: %s", exc)
        finally:
            logger.info("SyncHttpClient closed")

    @property
    def session(self) -> curl_requests.Session:
        if self._session is None:
            raise RuntimeError("Client not entered. Use 'with' context manager.")
        return self._session

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        # See the async twin: keep this out of RequestConfig.extra.
        session_key: str | None = kwargs.pop("session_key", None)
        config = self._build_config(method, url, **kwargs)
        config = self.middleware.process_request_sync(config)
        logger.debug("%s %s", config.method, config.url)

        proxies, proxy_url = self._resolve_proxies(session_key, self._host_of(config.url))
        request_kwargs = self._config_to_kwargs(config)
        if proxies is not None:
            request_kwargs["proxies"] = proxies

        def _send() -> Response:
            if self.rate_limiter is not None:
                self.rate_limiter.acquire(self._host_of(config.url))
            raw = self.session.request(
                cast("HttpMethod", config.method), config.url, **request_kwargs
            )
            return self._finalize(raw)

        try:
            if self.circuit_breaker is not None:
                resp = self.circuit_breaker.call(_send)
            else:
                resp = _send()
        # Inside the try, but *outside* the breaker. A backend that lies is not
        # a backend that is down: letting rejections trip the breaker deadlocks
        # recovery, because the warm-up request that would fix a stale identity
        # is itself refused by the open breaker. Being inside the try is what
        # matters - error hooks fire and a proxy-blaming rejection retires the
        # proxy, exactly as a transport failure would.
            if self.validate is not None:
                self.validate(resp)
        except BaseException as exc:
            mapped = map_transport_error(exc, url=config.url)
            self._handle_transport_failure(mapped, proxy_url)
            self.middleware.process_error_sync(mapped, config)
            raise mapped from exc

        logger.debug("%s %s -> %d", config.method, config.url, resp.status_code)
        return self.middleware.process_response_sync(resp)

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    def fetch_all(
        self,
        urls: Sequence[str],
        *,
        method: str = "GET",
        return_exceptions: bool = True,
        max_workers: int | None = None,
        **kwargs: Any,
    ) -> list[Response | BaseException]:
        """
        Fire many requests from a thread pool.

        curl_cffi sessions use thread-local handles, and the rotator/limiter are
        lock-guarded, so fanning out here is safe.
        """
        workers = max_workers or min(self.max_concurrent, max(len(urls), 1))
        results: list[Response | BaseException] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self.request, method, url, **kwargs) for url in urls]
            for fut in futures:
                try:
                    results.append(fut.result())
                except BaseException as exc:
                    if not return_exceptions:
                        raise
                    results.append(exc)
        return results


__all__ = ["AsyncHttpClient", "SyncHttpClient"]
