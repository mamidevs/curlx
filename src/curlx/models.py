"""
Typed response models and request configuration.
"""

from __future__ import annotations

import email.utils
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from curl_cffi import requests as curl_requests


@dataclass(frozen=True)
class RequestConfig:
    """
    Immutable request configuration.

    Middleware receives one of these and returns a (possibly new) instance;
    use :func:`dataclasses.replace` to derive a modified copy.
    """

    method: str = "GET"
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    json_data: Any | None = None
    data: Any | None = None
    cookies: Mapping[str, str] = field(default_factory=dict)
    timeout: float | None = None
    allow_redirects: Any = True
    verify: bool | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


def parse_retry_after(value: str | None) -> float | None:
    """
    Parse a ``Retry-After`` header into seconds.

    The header is either a delta in seconds or an HTTP-date (RFC 9110). Returns
    ``None`` when absent or unparseable, and never returns a negative delay.
    """
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return max(0.0, parsed.timestamp() - time.time())


class Response:
    """
    Thin wrapper around ``curl_cffi.requests.Response`` with extra utilities.
    """

    def __init__(self, raw: curl_requests.Response) -> None:
        self._raw = raw

    # ------------------------------------------------------------------
    # Passthrough properties
    # ------------------------------------------------------------------
    @property
    def status_code(self) -> int:
        return int(self._raw.status_code)

    @property
    def url(self) -> str:
        return str(self._raw.url)

    @property
    def headers(self) -> curl_requests.Headers:
        """Case-insensitive multi-dict of response headers, as curl_cffi built it."""
        return self._raw.headers

    @property
    def cookies(self) -> Mapping[str, str]:
        return self._raw.cookies

    @property
    def content(self) -> bytes:
        return self._raw.content

    @property
    def text(self) -> str:
        return self._raw.text

    @property
    def encoding(self) -> str | None:
        return getattr(self._raw, "encoding", None)

    @property
    def reason(self) -> str:
        """HTTP reason phrase, e.g. ``OK``."""
        return str(getattr(self._raw, "reason", "") or "")

    @property
    def elapsed(self) -> timedelta | None:
        """
        Request duration.

        curl_cffi switched this to a :class:`~datetime.timedelta` in 0.14; older
        builds exposed a float, which is normalized here.
        """
        value = getattr(self._raw, "elapsed", None)
        if value is None:
            return None
        if isinstance(value, timedelta):
            return value
        return timedelta(seconds=float(value))

    @property
    def redirect_count(self) -> int:
        return int(getattr(self._raw, "redirect_count", 0) or 0)

    @property
    def history(self) -> list[Any]:
        """Redirect responses that led here (bodies are not retained)."""
        return list(getattr(self._raw, "history", ()) or ())

    @property
    def primary_ip(self) -> str | None:
        return getattr(self._raw, "primary_ip", None)

    @property
    def http_version(self) -> Any | None:
        return getattr(self._raw, "http_version", None)

    @property
    def response_size(self) -> int | None:
        size = getattr(self._raw, "response_size", None)
        return int(size) if size is not None else None

    @property
    def retry_after(self) -> float | None:
        """``Retry-After`` in seconds, when the server sent one."""
        try:
            raw = self.headers.get("retry-after")
        except AttributeError:
            return None
        return parse_retry_after(raw)

    @property
    def raw_response(self) -> curl_requests.Response:
        """Access the underlying curl_cffi Response."""
        return self._raw

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def json(self, **kwargs: Any) -> Any:
        """
        Parse the response body as JSON.

        Decodes with the negotiated charset first so non-UTF-8 payloads survive;
        ``json.loads`` on raw bytes only ever tries UTF-8/16/32.
        """
        return json.loads(self.text, **kwargs)

    def raise_for_status(self) -> None:
        """Raise :class:`~curlx.exceptions.HttpStatusError` if status >= 400."""
        from curlx.exceptions import HttpStatusError

        if self.status_code >= 400:
            raise HttpStatusError(
                status_code=self.status_code,
                message=f"HTTP error: {self.status_code}",
                url=self.url,
                response_body=self.content,
                retry_after=self.retry_after,
            )

    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    def is_client_error(self) -> bool:
        return 400 <= self.status_code < 500

    def is_server_error(self) -> bool:
        return 500 <= self.status_code < 600

    def __repr__(self) -> str:
        return f"<Response [{self.status_code}] {self.url}>"


__all__ = ["RequestConfig", "Response", "parse_retry_after"]
