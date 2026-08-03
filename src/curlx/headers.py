"""
Header builder that stays coherent with the impersonation target.

curl_cffi already emits a full, correctly ordered header block matching whatever
``impersonate=`` target is in use, including ``User-Agent`` and the Chromium
client hints.  Those templates live in curl_cffi's compiled extension and cannot
be reproduced from Python, so :class:`HeaderBuilder` emits **no** ``User-Agent``
and **no** ``sec-ch-ua*`` headers unless a User-Agent is set explicitly.

Overriding only the User-Agent is what makes a client trivially detectable: the
TLS handshake says one browser while the request headers say another.  Leaving
those headers out lets curl_cffi's impersonation-matched values through intact.

To opt into a rotating User-Agent - for a plain transport with no impersonation,
where nothing else will supply one::

    HeaderBuilder().with_user_agent(rotate_user_agent("chrome"))

which also switches the client hints on, derived from that exact UA string.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, replace

from curlx.fingerprint import BrowserProfile, get_profile


@dataclass(frozen=True)
class UserAgentSpec:
    """
    A real User-Agent string paired with the curlx profile it is coherent with.

    ``profile`` is the key in :data:`curlx.fingerprint.SUPPORTED_PROFILES` whose
    TLS fingerprint matches this UA, so the two can be sent together.  It is
    ``None`` when curl_cffi has no matching target - currently only modern Edge,
    whose newest curl_cffi profile (``edge101``) is stuck on Chromium 101.
    """

    string: str
    profile: str | None


@dataclass(frozen=True)
class _Family:
    """Header traits shared by every User-Agent in a browser family."""

    accept: str
    accept_encoding: str
    user_agents: tuple[UserAgentSpec, ...]
    # Only Chromium-based browsers send sec-ch-ua*; brand is their vendor entry.
    brand: str | None = None


_CHROMIUM_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
_GECKO_WEBKIT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

_FAMILIES: dict[str, _Family] = {
    "chrome": _Family(
        accept=_CHROMIUM_ACCEPT,
        accept_encoding="gzip, deflate, br, zstd",
        brand="Google Chrome",
        user_agents=(
            UserAgentSpec(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",  # noqa: E501
                "chrome146",
            ),
            UserAgentSpec(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",  # noqa: E501
                "chrome146",
            ),
            UserAgentSpec(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",  # noqa: E501
                "chrome145",
            ),
            UserAgentSpec(
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",  # noqa: E501
                "chrome131_android",
            ),
        ),
    ),
    "firefox": _Family(
        accept=_GECKO_WEBKIT_ACCEPT,
        # Firefox has shipped zstd since 126.
        accept_encoding="gzip, deflate, br, zstd",
        user_agents=(
            UserAgentSpec(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
                "firefox147",
            ),
            UserAgentSpec(
                "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
                "firefox147",
            ),
            UserAgentSpec(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0",  # noqa: E501
                "firefox144",
            ),
        ),
    ),
    "safari": _Family(
        accept=_GECKO_WEBKIT_ACCEPT,
        # Safari still does not offer zstd.
        accept_encoding="gzip, deflate, br",
        user_agents=(
            UserAgentSpec(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",  # noqa: E501
                "safari260",
            ),
            UserAgentSpec(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",  # noqa: E501
                "safari184",
            ),
            UserAgentSpec(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",  # noqa: E501
                "safari184_ios",
            ),
        ),
    ),
    "edge": _Family(
        accept=_CHROMIUM_ACCEPT,
        accept_encoding="gzip, deflate, br, zstd",
        brand="Microsoft Edge",
        # Real Edge keeps its Chromium and Edg majors in lockstep.  No profile
        # pairing is possible: curl_cffi's newest Edge target is Chromium 101,
        # so an Edge UA cannot be made coherent with an Edge impersonation.
        user_agents=(
            UserAgentSpec(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",  # noqa: E501
                None,
            ),
            UserAgentSpec(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",  # noqa: E501
                None,
            ),
        ),
    ),
}

_DEFAULT_FAMILY = "chrome"

# Longest prefix wins, so chrome_android resolves before any shorter key could
# claim it.  Tor Browser is a Firefox fork and shares its header shape.
_PROFILE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("chrome", "chrome"),
    ("edge", "edge"),
    ("safari", "safari"),
    ("firefox", "firefox"),
    ("tor", "firefox"),
)

# Checked in order: Android and iOS UAs also mention Linux / Mac OS X.
_PLATFORM_MARKERS: tuple[tuple[str, str], ...] = (
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Windows", "Windows"),
    ("CrOS", "Chrome OS"),
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("X11", "Linux"),
    ("Linux", "Linux"),
)

_CHROMIUM_MAJOR_RE = re.compile(r"Chrome/(\d+)")

# Chrome's GREASE brand is randomised per build and the real per-target value
# lives inside curl_cffi's compiled extension, so this is a best-effort
# approximation - which is exactly why no client hints are sent by default.
_GREASE_BRAND = '"Not_A Brand";v="24"'


def _get_family(browser: str) -> _Family:
    family = _FAMILIES.get(browser.lower())
    if family is None:
        raise ValueError(
            f"Unsupported browser: {browser!r}. Use one of: {', '.join(_FAMILIES)}"
        )
    return family


def _family_name_for_profile(impersonate: str) -> str:
    for prefix, family in sorted(_PROFILE_FAMILIES, key=lambda pair: -len(pair[0])):
        if impersonate.startswith(prefix):
            return family
    return _DEFAULT_FAMILY


def _platform_of(ua: str) -> str:
    for marker, platform in _PLATFORM_MARKERS:
        if marker in ua:
            return platform
    return "Windows"


def _client_hints(ua: str, family: _Family) -> dict[str, str]:
    """Build sec-ch-ua* consistent with ``ua``; empty for non-Chromium UAs."""
    match = _CHROMIUM_MAJOR_RE.search(ua)
    if family.brand is None or match is None:
        return {}
    major = match.group(1)
    return {
        "sec-ch-ua": f'"{family.brand}";v="{major}", "Chromium";v="{major}", {_GREASE_BRAND}',
        "sec-ch-ua-mobile": "?1" if "Mobile" in ua else "?0",
        "sec-ch-ua-platform": f'"{_platform_of(ua)}"',
    }


@dataclass(frozen=True)
class HeaderBuilder:
    """
    Immutable fluent builder for realistic HTTP headers.

    Every ``with_*`` returns a new instance, so a partly configured builder can
    be shared and branched safely.

    ``User-Agent`` and the client hints are omitted unless :meth:`with_user_agent`
    was called - see the module docstring for why.  ``sec-ch-ua-platform`` is
    then derived from that UA rather than assumed, unless :meth:`with_platform`
    overrides it.

    The header family (``Accept``, ``Accept-Encoding``) comes from ``browser``
    when set, otherwise from ``profile``, otherwise Chrome.

    ``Sec-Fetch-Site`` defaults to ``none`` - a navigation with no initiator -
    and drops to ``cross-site`` as soon as a referer is set, since ``none`` and
    a ``Referer`` never occur together in a real browser.
    """

    browser: str | None = None
    profile: BrowserProfile | None = None
    user_agent: str | None = None
    referer: str | None = None
    accept_language: str = "en-US,en;q=0.9"
    platform: str | None = None
    # Derived when unset: "none" means the request had no initiator at all
    # (address bar, bookmark), which cannot co-exist with a Referer.  Setting a
    # referer therefore floors this at "cross-site" - the builder has no request
    # URL, so it cannot tell same-origin from same-site.  Pass the accurate
    # value to with_sec_fetch_site() when you do know it.
    sec_fetch_site: str | None = None
    # Stored as pairs rather than a dict so instances never share mutable state.
    extra_headers: tuple[tuple[str, str], ...] = ()

    def with_browser(self, browser: str) -> HeaderBuilder:
        _get_family(browser)
        return replace(self, browser=browser.lower())

    def with_profile(self, profile: str | BrowserProfile) -> HeaderBuilder:
        """Derive the header family from an impersonation profile."""
        resolved = get_profile(profile) if isinstance(profile, str) else profile
        return replace(self, profile=resolved)

    def with_user_agent(self, ua: str) -> HeaderBuilder:
        """Send an explicit User-Agent, plus client hints derived from it."""
        return replace(self, user_agent=ua)

    def with_referer(self, referer: str) -> HeaderBuilder:
        return replace(self, referer=referer)

    def with_accept_language(self, lang: str) -> HeaderBuilder:
        return replace(self, accept_language=lang)

    def with_platform(self, platform: str) -> HeaderBuilder:
        """Override the sec-ch-ua-platform that would be derived from the UA."""
        return replace(self, platform=platform)

    def with_sec_fetch_site(self, site: str) -> HeaderBuilder:
        return replace(self, sec_fetch_site=site)

    def with_header(self, key: str, value: str) -> HeaderBuilder:
        return replace(self, extra_headers=(*self.extra_headers, (key, value)))

    def with_headers(self, headers: dict[str, str]) -> HeaderBuilder:
        return replace(self, extra_headers=self.extra_headers + tuple(headers.items()))

    def _family(self) -> _Family:
        if self.browser is not None:
            return _get_family(self.browser)
        if self.profile is not None:
            return _FAMILIES[_family_name_for_profile(self.profile.impersonate)]
        return _FAMILIES[_DEFAULT_FAMILY]

    def build(self) -> dict[str, str]:
        family = self._family()
        headers: dict[str, str] = {
            "Accept": family.accept,
            "Accept-Language": self.accept_language,
            "Accept-Encoding": family.accept_encoding,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": self.sec_fetch_site or ("cross-site" if self.referer else "none"),
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }

        # Left out entirely when no UA was set, so curl_cffi's own
        # impersonation-matched User-Agent and client hints survive.
        if self.user_agent is not None:
            headers["User-Agent"] = self.user_agent
            hints = _client_hints(self.user_agent, family)
            if hints and self.platform is not None:
                hints["sec-ch-ua-platform"] = f'"{self.platform}"'
            headers.update(hints)

        if self.referer:
            headers["Referer"] = self.referer

        headers.update(self.extra_headers)
        return headers


def rotate_user_agent(browser: str = "chrome") -> str:
    """
    Return a random User-Agent for the given browser family.

    Raises ``ValueError`` for an unknown family, matching
    :meth:`HeaderBuilder.with_browser` rather than silently serving Chrome.
    """
    return random.choice(_get_family(browser).user_agents).string


def list_user_agents(browser: str = "chrome") -> list[UserAgentSpec]:
    """Return the User-Agent pool for a family, each paired with its profile."""
    return list(_get_family(browser).user_agents)


def add_timestamp_headers(headers: dict[str, str]) -> dict[str, str]:
    """Add dynamic timestamp-based headers (e.g. for API fingerprint evasion)."""
    headers = dict(headers)
    ms = int(time.time() * 1000)
    headers["X-Request-Timestamp"] = str(ms)
    return headers
