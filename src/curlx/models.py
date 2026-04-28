"""
Typed response models and request configuration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from curl_cffi import requests as curl_requests


@dataclass
class RequestConfig:
    """Immutable request configuration."""

    method: str = "GET"
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    json_data: Optional[Any] = None
    data: Optional[Any] = None
    cookies: Dict[str, str] = field(default_factory=dict)
    timeout: Optional[float] = None
    allow_redirects: bool = True
    verify: Optional[bool] = None


class Response:
    """
    Thin wrapper around curl_cffi.Response with extra utilities.
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
    def headers(self) -> Any:
        return self._raw.headers

    @property
    def cookies(self) -> Any:
        return self._raw.cookies

    @property
    def content(self) -> bytes:
        return self._raw.content

    @property
    def text(self) -> str:
        return self._raw.text

    @property
    def encoding(self) -> Optional[str]:
        return getattr(self._raw, "encoding", None)

    @property
    def raw_response(self) -> curl_requests.Response:
        """Access the underlying curl_cffi Response."""
        return self._raw

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def json(self, **kwargs: Any) -> Any:
        """Parse response body as JSON."""
        return json.loads(self.content, **kwargs)

    def raise_for_status(self) -> None:
        """Raise HttpStatusError if status >= 400."""
        from curlx.exceptions import HttpStatusError

        if self.status_code >= 400:
            raise HttpStatusError(
                status_code=self.status_code,
                message=f"HTTP error: {self.status_code}",
                url=self.url,
                response_body=self.content,
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
