"""
AsyncHttpClient & SyncHttpClient – the main entry points.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Union

from curl_cffi import requests as curl_requests

from curlx.exceptions import HttpStatusError, ProxyError
from curlx.fingerprint import BrowserProfile, get_profile
from curlx.middleware import MiddlewareChain
from curlx.models import RequestConfig, Response
from curlx.proxy import ProxyRotator
from curlx.retry import CircuitBreaker
from curlx.session import SessionFactory
from curlx.utils import get_logger, merge_dicts

logger = get_logger("curlx.client")


class _BaseClient:
    """Shared logic between sync and async clients."""

    def __init__(
        self,
        *,
        max_concurrent: int = 100,
        impersonate: str = "chrome136",
        proxies: Optional[Union[str, Dict[str, str], ProxyRotator]] = None,
        timeout: float = 30.0,
        verify: bool = True,
        max_clients: Optional[int] = None,
        default_headers: Optional[Dict[str, str]] = None,
        middleware: Optional[MiddlewareChain] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.max_concurrent = max(max_concurrent, 1)
        self.max_clients = max_clients or self.max_concurrent
        self.timeout = timeout
        self.verify = verify
        self.default_headers = default_headers or {}
        self.middleware = middleware or MiddlewareChain()
        self.circuit_breaker = circuit_breaker

        self.profile = get_profile(impersonate)

        # Normalize proxies
        if proxies is None:
            self.proxy_rotator: Optional[ProxyRotator] = None
        elif isinstance(proxies, ProxyRotator):
            self.proxy_rotator = proxies
        elif isinstance(proxies, str):
            self.proxy_rotator = ProxyRotator([proxies])
        elif isinstance(proxies, dict):
            # curl_cffi style dict
            self.proxy_rotator = None
            self._static_proxies = proxies
        else:
            raise ProxyError(f"Unsupported proxies type: {type(proxies)}")

        self._session_factory = SessionFactory(
            impersonate=self.profile,
            proxies=self.proxy_rotator,
            timeout=timeout,
            verify=verify,
            max_clients=self.max_clients,
            default_headers=self.default_headers,
        )

    def _resolve_proxies(self, session_key: Optional[str] = None) -> Optional[Dict[str, str]]:
        if hasattr(self, "_static_proxies"):
            return self._static_proxies
        if self.proxy_rotator:
            return self.proxy_rotator.get(session_key=session_key)
        return None

    def _prepare_kwargs(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        merged_headers = merge_dicts(self.default_headers, headers)
        req_config = RequestConfig(
            method=method.upper(),
            url=url,
            headers=merged_headers,
        )

        request_kwargs: Dict[str, Any] = {
            "headers": merged_headers or None,
            "timeout": kwargs.pop("timeout", self.timeout),
            "verify": kwargs.pop("verify", self.verify),
            "allow_redirects": kwargs.pop("allow_redirects", True),
        }

        if "params" in kwargs:
            request_kwargs["params"] = kwargs.pop("params")
        if "json" in kwargs:
            request_kwargs["json"] = kwargs.pop("json")
        if "data" in kwargs:
            request_kwargs["data"] = kwargs.pop("data")
        if "cookies" in kwargs:
            request_kwargs["cookies"] = kwargs.pop("cookies")

        # Pass any remaining curl_cffi-specific kwargs
        request_kwargs.update(kwargs)
        return request_kwargs


class AsyncHttpClient(_BaseClient):
    """
    Async HTTP client with semaphore-based concurrency.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._session: Optional[curl_requests.AsyncSession] = None

    async def __aenter__(self) -> AsyncHttpClient:
        self._session = self._session_factory.create_async_session()
        logger.info(
            "AsyncHttpClient started | max_concurrent=%d impersonate=%s",
            self.max_concurrent,
            self.profile.name,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("AsyncHttpClient closed")

    @property
    def session(self) -> curl_requests.AsyncSession:
        if self._session is None:
            raise RuntimeError("Client not entered. Use 'async with' context manager.")
        return self._session

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        request_kwargs = self._prepare_kwargs(method, url, **kwargs)
        logger.debug("%s %s", method.upper(), url)

        async with self._semaphore:
            raw = await self.session.request(method, url, **request_kwargs)
        resp = Response(raw)
        logger.debug("%s %s -> %d", method.upper(), url, resp.status_code)
        return resp

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
        urls: list,
        *,
        method: str = "GET",
        return_exceptions: bool = True,
        **kwargs: Any,
    ) -> list:
        """
        Fire many requests concurrently and gather results.
        Semaphore still applies, so only max_concurrent run at once.
        """
        tasks = [self.request(method, url, **kwargs) for url in urls]
        return await asyncio.gather(*tasks, return_exceptions=return_exceptions)


class SyncHttpClient(_BaseClient):
    """
    Synchronous HTTP client with optional thread-pool concurrency.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session: Optional[curl_requests.Session] = None

    def __enter__(self) -> SyncHttpClient:
        self._session = self._session_factory.create_sync_session()
        logger.info(
            "SyncHttpClient started | impersonate=%s",
            self.profile.name,
        )
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._session:
            self._session.close()
            self._session = None
            logger.info("SyncHttpClient closed")

    @property
    def session(self) -> curl_requests.Session:
        if self._session is None:
            raise RuntimeError("Client not entered. Use 'with' context manager.")
        return self._session

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        request_kwargs = self._prepare_kwargs(method, url, **kwargs)
        logger.debug("%s %s", method.upper(), url)
        raw = self.session.request(method, url, **request_kwargs)
        resp = Response(raw)
        logger.debug("%s %s -> %d", method.upper(), url, resp.status_code)
        return resp

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
