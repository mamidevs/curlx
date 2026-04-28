"""
Request/response middleware / interceptors.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List, Optional, Union

from curlx.models import RequestConfig, Response

# Type aliases
RequestHook = Callable[[RequestConfig], Union[RequestConfig, Awaitable[RequestConfig]]]
ResponseHook = Callable[[Response], Union[Response, Awaitable[Response]]]
ErrorHook = Callable[[Exception, RequestConfig], Union[None, Awaitable[None]]]


class MiddlewareChain:
    """Chainable middleware for request/response lifecycle."""

    def __init__(self) -> None:
        self._request_hooks: List[RequestHook] = []
        self._response_hooks: List[ResponseHook] = []
        self._error_hooks: List[ErrorHook] = []

    def add_request_hook(self, hook: RequestHook) -> MiddlewareChain:
        self._request_hooks.append(hook)
        return self

    def add_response_hook(self, hook: ResponseHook) -> MiddlewareChain:
        self._response_hooks.append(hook)
        return self

    def add_error_hook(self, hook: ErrorHook) -> MiddlewareChain:
        self._error_hooks.append(hook)
        return self

    async def process_request(self, config: RequestConfig) -> RequestConfig:
        for hook in self._request_hooks:
            result = hook(config)
            if hasattr(result, "__await__"):
                config = await result
            else:
                config = result
        return config

    async def process_response(self, response: Response) -> Response:
        for hook in self._response_hooks:
            result = hook(response)
            if hasattr(result, "__await__"):
                response = await result
            else:
                response = result
        return response

    async def process_error(self, exc: Exception, config: RequestConfig) -> None:
        for hook in self._error_hooks:
            result = hook(exc, config)
            if hasattr(result, "__await__"):
                await result
