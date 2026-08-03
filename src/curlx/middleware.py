"""
Request/response middleware / interceptors.

Hooks may be plain callables or coroutine functions. The async client drives the
chain through :meth:`MiddlewareChain.process_request` and friends; the sync
client uses the ``*_sync`` variants, which reject async hooks with an explicit
error instead of silently skipping them.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from curlx.exceptions import CurlxError
from curlx.models import RequestConfig, Response

# Type aliases
RequestHook = Callable[[RequestConfig], RequestConfig | Awaitable[RequestConfig]]
ResponseHook = Callable[[Response], Response | Awaitable[Response]]
ErrorHook = Callable[[Exception, RequestConfig], Awaitable[None] | None]


def _hook_name(hook: object) -> str:
    """Best-effort identifier for error messages."""
    return getattr(hook, "__qualname__", None) or getattr(hook, "__name__", None) or repr(hook)


def _reject_awaitable(result: Any, hook: object, kind: str) -> None:
    """
    Refuse an awaitable handed to a synchronous caller.

    Skipping it would drop the hook's effect entirely; awaiting it is not
    possible without an event loop, so the only honest option is to fail loudly.
    """
    if not inspect.isawaitable(result):
        return
    if inspect.iscoroutine(result):
        # Otherwise Python emits a bare "coroutine was never awaited" warning
        # that obscures the real error below.
        result.close()
    raise CurlxError(
        f"{kind} hook {_hook_name(hook)!r} is asynchronous and cannot run on the "
        f"synchronous client. Use AsyncHttpClient, or register a non-async hook."
    )


def _require_value(result: Any, hook: object, kind: str) -> Any:
    """Reject a hook that forgot to return the object it was handed."""
    if result is None:
        raise CurlxError(
            f"{kind} hook {_hook_name(hook)!r} returned None. A {kind.lower()} hook "
            f"must return the (possibly modified) object it received."
        )
    return result


class MiddlewareChain:
    """Chainable middleware for request/response lifecycle."""

    def __init__(self) -> None:
        self._request_hooks: list[RequestHook] = []
        self._response_hooks: list[ResponseHook] = []
        self._error_hooks: list[ErrorHook] = []

    def add_request_hook(self, hook: RequestHook) -> MiddlewareChain:
        self._request_hooks.append(hook)
        return self

    def add_response_hook(self, hook: ResponseHook) -> MiddlewareChain:
        self._response_hooks.append(hook)
        return self

    def add_error_hook(self, hook: ErrorHook) -> MiddlewareChain:
        self._error_hooks.append(hook)
        return self

    # ------------------------------------------------------------------
    # Async path
    # ------------------------------------------------------------------
    async def process_request(self, config: RequestConfig) -> RequestConfig:
        for hook in self._request_hooks:
            result: Any = hook(config)
            if inspect.isawaitable(result):
                result = await result
            config = _require_value(result, hook, "Request")
        return config

    async def process_response(self, response: Response) -> Response:
        for hook in self._response_hooks:
            result: Any = hook(response)
            if inspect.isawaitable(result):
                result = await result
            response = _require_value(result, hook, "Response")
        return response

    async def process_error(self, exc: Exception, config: RequestConfig) -> None:
        for hook in self._error_hooks:
            result = hook(exc, config)
            if inspect.isawaitable(result):
                await result

    # ------------------------------------------------------------------
    # Sync path
    # ------------------------------------------------------------------
    def process_request_sync(self, config: RequestConfig) -> RequestConfig:
        for hook in self._request_hooks:
            result: Any = hook(config)
            _reject_awaitable(result, hook, "Request")
            config = _require_value(result, hook, "Request")
        return config

    def process_response_sync(self, response: Response) -> Response:
        for hook in self._response_hooks:
            result: Any = hook(response)
            _reject_awaitable(result, hook, "Response")
            response = _require_value(result, hook, "Response")
        return response

    def process_error_sync(self, exc: Exception, config: RequestConfig) -> None:
        # Error hooks legitimately return None, so only the async check applies.
        for hook in self._error_hooks:
            _reject_awaitable(hook(exc, config), hook, "Error")


__all__ = ["ErrorHook", "MiddlewareChain", "RequestHook", "ResponseHook"]
