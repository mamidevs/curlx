"""
Shared test doubles, exposed as fixtures.

The curl_cffi response duck-type was hand-copied into four test modules, which
meant a change to what curlx reads off a response had to be mirrored four times
- and the copies had already drifted (one carried ``encoding``, the others did
not). One definition here, reached through fixtures rather than imports:
pytest loads a conftest under its own module name, so ``from conftest import
...`` is not reliably available to test modules.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class FakeRawResponse:
    """Duck-type of curl_cffi's Response, limited to what curlx reads."""

    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b'{"ok": true}',
        url: str = "https://example.com/test",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.url = url
        self.headers: dict[str, str] = headers or {}
        self.cookies: dict[str, str] = {}
        self.text = content.decode()
        self.encoding = "utf-8"
        self.reason = "OK"
        self.elapsed = 0.0
        self.redirect_count = 0
        self.history: list[Any] = []
        self.primary_ip = "127.0.0.1"
        self.http_version = 2


@pytest.fixture
def fake_response() -> type[FakeRawResponse]:
    """The duck-type class itself, for tests that build their own instances."""
    return FakeRawResponse


@pytest.fixture
def patch_async():
    """
    Factory stubbing an *entered* async client's transport.

    Returns ``(patcher, mock)``; the caller enters the patcher. A factory
    rather than a plain fixture because the client has to exist first.
    """

    def _patch(client: Any, *, result: Any = None, side_effect: Any = None):
        mock = AsyncMock(return_value=result if result is not None else FakeRawResponse())
        if side_effect is not None:
            mock.side_effect = side_effect
        return patch.object(client.session, "request", mock), mock

    return _patch


@pytest.fixture
def patch_sync():
    """Synchronous twin of :func:`patch_async`."""

    def _patch(client: Any, *, result: Any = None, side_effect: Any = None):
        mock = MagicMock(return_value=result if result is not None else FakeRawResponse())
        if side_effect is not None:
            mock.side_effect = side_effect
        return patch.object(client.session, "request", mock), mock

    return _patch
