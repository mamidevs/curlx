"""
Browser fingerprint presets for curl_cffi impersonation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class BrowserProfile:
    """A complete browser fingerprint profile."""

    name: str
    impersonate: str
    ja3: Optional[str] = None
    akamai: Optional[str] = None
    http2: Optional[bool] = None


# curl_cffi supports these impersonate targets (as of 0.10+).
# Extend as new browsers are supported.
SUPPORTED_PROFILES: Dict[str, BrowserProfile] = {
    "chrome110": BrowserProfile("Chrome 110", "chrome110"),
    "chrome116": BrowserProfile("Chrome 116", "chrome116"),
    "chrome119": BrowserProfile("Chrome 119", "chrome119"),
    "chrome120": BrowserProfile("Chrome 120", "chrome120"),
    "chrome123": BrowserProfile("Chrome 123", "chrome123"),
    "chrome124": BrowserProfile("Chrome 124", "chrome124"),
    "chrome131": BrowserProfile("Chrome 131", "chrome131"),
    "chrome133": BrowserProfile("Chrome 133", "chrome133"),
    "chrome137": BrowserProfile("Chrome 137", "chrome137"),
    "edge99": BrowserProfile("Edge 99", "edge99"),
    "edge101": BrowserProfile("Edge 101", "edge101"),
    "safari15_3": BrowserProfile("Safari 15.3", "safari15_3"),
    "safari15_5": BrowserProfile("Safari 15.5", "safari15_5"),
    "safari17_0": BrowserProfile("Safari 17.0", "safari17_0"),
    "safari17_2_ios": BrowserProfile("Safari 17.2 iOS", "safari17_2_ios"),
    "firefox91esr": BrowserProfile("Firefox 91 ESR", "firefox91esr"),
    "firefox109": BrowserProfile("Firefox 109", "firefox109"),
    "firefox117": BrowserProfile("Firefox 117", "firefox117"),
    "firefox128": BrowserProfile("Firefox 128", "firefox128"),
}


def get_profile(name: str) -> BrowserProfile:
    """Get a browser profile by name."""
    profile = SUPPORTED_PROFILES.get(name)
    if not profile:
        raise ValueError(
            f"Unknown impersonate profile: {name!r}. "
            f"Supported: {', '.join(SUPPORTED_PROFILES)}"
        )
    return profile


def list_profiles() -> List[str]:
    """Return all supported impersonate profile names."""
    return list(SUPPORTED_PROFILES.keys())
