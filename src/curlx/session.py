"""
Session lifecycle management wrapping curl_cffi sessions.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from curl_cffi import requests as curl_requests

from curlx.fingerprint import BrowserProfile, get_profile
from curlx.proxy import ProxyRotator
from curlx.utils import get_logger, merge_dicts

logger = get_logger("curlx.session")


class SessionFactory:
    """
    Factory for creating curl_cffi sessions with consistent settings.
    """

    def __init__(
        self,
        *,
        impersonate: Union[str, BrowserProfile] = "chrome137",
        proxies: Optional[Union[Dict[str, str], ProxyRotator]] = None,
        timeout: float = 30.0,
        verify: bool = True,
        max_clients: int = 100,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.profile = (
            get_profile(impersonate)
            if isinstance(impersonate, str)
            else impersonate
        )
        self.proxies = proxies
        self.timeout = timeout
        self.verify = verify
        self.max_clients = max_clients
        self.default_headers = default_headers or {}

    def _resolve_proxies(
        self, session_key: Optional[str] = None
    ) -> Optional[Dict[str, str]]:
        if self.proxies is None:
            return None
        if isinstance(self.proxies, ProxyRotator):
            return self.proxies.get(session_key=session_key)
        return self.proxies

    def create_sync_session(
        self, extra_headers: Optional[Dict[str, str]] = None
    ) -> curl_requests.Session:
        proxies = self._resolve_proxies()
        headers = merge_dicts(self.default_headers, extra_headers)
        session = curl_requests.Session(
            impersonate=self.profile.impersonate,
            proxies=proxies,
            timeout=self.timeout,
            verify=self.verify,
            headers=headers or None,
        )
        logger.debug("Created sync session | impersonate=%s", self.profile.name)
        return session

    def create_async_session(
        self,
        extra_headers: Optional[Dict[str, str]] = None,
        session_key: Optional[str] = None,
    ) -> curl_requests.AsyncSession:
        proxies = self._resolve_proxies(session_key=session_key)
        headers = merge_dicts(self.default_headers, extra_headers)
        session = curl_requests.AsyncSession(
            impersonate=self.profile.impersonate,
            proxies=proxies,
            timeout=self.timeout,
            verify=self.verify,
            max_clients=self.max_clients,
            headers=headers or None,
        )
        logger.debug("Created async session | impersonate=%s", self.profile.name)
        return session
