"""
Dynamic header builder with real browser User-Agent rotation.
"""

from __future__ import annotations

import random
import time
from typing import Dict, List, Optional


# Real browser User-Agent strings (kept up-to-date periodically)
_CHROME_UAS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
]

_FIREFOX_UAS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

_SAFARI_UAS: List[str] = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

_EDGE_UAS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

_BROWSER_MAP = {
    "chrome": _CHROME_UAS,
    "firefox": _FIREFOX_UAS,
    "safari": _SAFARI_UAS,
    "edge": _EDGE_UAS,
}


class HeaderBuilder:
    """
    Fluent API for building realistic HTTP headers.
    """

    def __init__(self) -> None:
        self._headers: Dict[str, str] = {}
        self._browser: str = "chrome"
        self._custom_ua: Optional[str] = None
        self._referer: Optional[str] = None
        self._accept_language: str = "en-US,en;q=0.9"
        self._platform: str = "Windows"

    def with_browser(self, browser: str) -> HeaderBuilder:
        browser = browser.lower()
        if browser not in _BROWSER_MAP:
            raise ValueError(f"Unsupported browser: {browser}. Use: {list(_BROWSER_MAP)}")
        self._browser = browser
        return self

    def with_user_agent(self, ua: str) -> HeaderBuilder:
        self._custom_ua = ua
        return self

    def with_referer(self, referer: str) -> HeaderBuilder:
        self._referer = referer
        return self

    def with_accept_language(self, lang: str) -> HeaderBuilder:
        self._accept_language = lang
        return self

    def with_platform(self, platform: str) -> HeaderBuilder:
        self._platform = platform
        return self

    def with_header(self, key: str, value: str) -> HeaderBuilder:
        self._headers[key] = value
        return self

    def with_headers(self, headers: Dict[str, str]) -> HeaderBuilder:
        self._headers.update(headers)
        return self

    def _pick_ua(self) -> str:
        if self._custom_ua:
            return self._custom_ua
        pool = _BROWSER_MAP.get(self._browser, _CHROME_UAS)
        return random.choice(pool)

    def build(self) -> Dict[str, str]:
        ua = self._pick_ua()
        headers: Dict[str, str] = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self._accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }

        # Chrome-specific Sec-CH-UA
        if self._browser == "chrome":
            headers["sec-ch-ua"] = '"Chromium";v="137", "Not(A:Brand";v="24"'
            headers["sec-ch-ua-mobile"] = "?0"
            headers["sec-ch-ua-platform"] = f'"{self._platform}"'

        if self._referer:
            headers["Referer"] = self._referer

        headers.update(self._headers)
        return headers


def rotate_user_agent(browser: str = "chrome") -> str:
    """Return a random User-Agent for the given browser family."""
    pool = _BROWSER_MAP.get(browser, _CHROME_UAS)
    return random.choice(pool)


def add_timestamp_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Add dynamic timestamp-based headers (e.g. for API fingerprint evasion)."""
    headers = dict(headers)
    ms = int(time.time() * 1000)
    headers["X-Request-Timestamp"] = str(ms)
    return headers
