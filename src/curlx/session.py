"""
Session lifecycle management wrapping curl_cffi sessions.

Proxies are deliberately *not* baked into the session here. The clients resolve
a proxy per request and pass it to ``session.request(proxies=...)``, which is
what makes rotation actually rotate.
"""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as curl_requests

from curlx.fingerprint import BrowserProfile, get_profile
from curlx.utils import get_logger, merge_dicts

logger = get_logger("curlx.session")

# curl_cffi accepts True/False, a CurlFollow member, or the string "safe".
# "safe" rejects redirects to internal/private IPs (GHSA-qw2m-4pqf-rmpp).
SAFE_REDIRECTS = "safe"


class SessionFactory:
    """
    Factory for creating curl_cffi sessions with consistent settings.
    """

    def __init__(
        self,
        *,
        impersonate: str | BrowserProfile = "chrome",
        timeout: float = 30.0,
        verify: bool = True,
        max_clients: int = 100,
        default_headers: dict[str, str] | None = None,
        allow_redirects: Any = SAFE_REDIRECTS,
        max_redirects: int | None = None,
        header_order: str | None = None,
        doh_url: str | None = None,
        interface: str | None = None,
        cert: Any | None = None,
        http_version: Any | None = None,
    ) -> None:
        self.profile = get_profile(impersonate) if isinstance(impersonate, str) else impersonate
        self.timeout = timeout
        self.verify = verify
        self.max_clients = max_clients
        self.default_headers = default_headers or {}
        self.allow_redirects = allow_redirects
        self.max_redirects = max_redirects
        self.header_order = header_order
        self.doh_url = doh_url
        self.interface = interface
        self.cert = cert
        self.http_version = http_version

    def _curl_options(self) -> dict[Any, Any] | None:
        """
        Build raw libcurl options.

        ``header_order`` has no dedicated kwarg; curl_cffi exposes it as
        ``CurlOpt.HTTPHEADER_ORDER`` and expects a comma-separated header list,
        e.g. ``"Host,Cookie,User-Agent"``.
        """
        if not self.header_order:
            return None
        from curl_cffi.const import CurlOpt

        return {CurlOpt.HTTPHEADER_ORDER: self.header_order}

    def _common_kwargs(self, extra_headers: dict[str, str] | None) -> dict[str, Any]:
        headers = merge_dicts(self.default_headers, extra_headers)
        kwargs: dict[str, Any] = {
            "impersonate": self.profile.impersonate,
            "timeout": self.timeout,
            "verify": self.verify,
            "headers": headers or None,
            "allow_redirects": self.allow_redirects,
        }
        # Only forward optionals that were actually set, so curl_cffi keeps its
        # own defaults for everything else.
        optional = {
            "max_redirects": self.max_redirects,
            "ja3": getattr(self.profile, "ja3", None),
            "akamai": getattr(self.profile, "akamai", None),
            "doh_url": self.doh_url,
            "interface": self.interface,
            "cert": self.cert,
            "http_version": self.http_version,
            "curl_options": self._curl_options(),
        }
        kwargs.update({k: v for k, v in optional.items() if v is not None})
        return kwargs

    def create_sync_session(
        self, extra_headers: dict[str, str] | None = None
    ) -> curl_requests.Session:
        session = curl_requests.Session(**self._common_kwargs(extra_headers))
        logger.debug("Created sync session | impersonate=%s", self.profile.name)
        return session

    def create_async_session(
        self, extra_headers: dict[str, str] | None = None
    ) -> curl_requests.AsyncSession:
        session = curl_requests.AsyncSession(
            max_clients=self.max_clients, **self._common_kwargs(extra_headers)
        )
        logger.debug("Created async session | impersonate=%s", self.profile.name)
        return session
