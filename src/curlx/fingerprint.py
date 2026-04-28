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


# curl_cffi 0.15+ supported impersonate targets.
# Kept in sync with curl_cffi.requests.BrowserType
SUPPORTED_PROFILES: Dict[str, BrowserProfile] = {
    # Chrome
    "chrome99": BrowserProfile("Chrome 99", "chrome99"),
    "chrome100": BrowserProfile("Chrome 100", "chrome100"),
    "chrome101": BrowserProfile("Chrome 101", "chrome101"),
    "chrome104": BrowserProfile("Chrome 104", "chrome104"),
    "chrome107": BrowserProfile("Chrome 107", "chrome107"),
    "chrome110": BrowserProfile("Chrome 110", "chrome110"),
    "chrome116": BrowserProfile("Chrome 116", "chrome116"),
    "chrome119": BrowserProfile("Chrome 119", "chrome119"),
    "chrome120": BrowserProfile("Chrome 120", "chrome120"),
    "chrome123": BrowserProfile("Chrome 123", "chrome123"),
    "chrome124": BrowserProfile("Chrome 124", "chrome124"),
    "chrome131": BrowserProfile("Chrome 131", "chrome131"),
    "chrome133a": BrowserProfile("Chrome 133a", "chrome133a"),
    "chrome136": BrowserProfile("Chrome 136", "chrome136"),
    "chrome142": BrowserProfile("Chrome 142", "chrome142"),
    "chrome145": BrowserProfile("Chrome 145", "chrome145"),
    "chrome146": BrowserProfile("Chrome 146", "chrome146"),
    "chrome99_android": BrowserProfile("Chrome 99 Android", "chrome99_android"),
    "chrome131_android": BrowserProfile("Chrome 131 Android", "chrome131_android"),
    # Edge
    "edge99": BrowserProfile("Edge 99", "edge99"),
    "edge101": BrowserProfile("Edge 101", "edge101"),
    # Safari
    "safari15_3": BrowserProfile("Safari 15.3", "safari15_3"),
    "safari15_5": BrowserProfile("Safari 15.5", "safari15_5"),
    "safari17_0": BrowserProfile("Safari 17.0", "safari17_0"),
    "safari17_2_ios": BrowserProfile("Safari 17.2 iOS", "safari17_2_ios"),
    "safari18_0": BrowserProfile("Safari 18.0", "safari18_0"),
    "safari18_0_ios": BrowserProfile("Safari 18.0 iOS", "safari18_0_ios"),
    "safari153": BrowserProfile("Safari 15.3", "safari153"),
    "safari155": BrowserProfile("Safari 15.5", "safari155"),
    "safari170": BrowserProfile("Safari 17.0", "safari170"),
    "safari172_ios": BrowserProfile("Safari 17.2 iOS", "safari172_ios"),
    "safari180": BrowserProfile("Safari 18.0", "safari180"),
    "safari180_ios": BrowserProfile("Safari 18.0 iOS", "safari180_ios"),
    "safari184": BrowserProfile("Safari 18.4", "safari184"),
    "safari184_ios": BrowserProfile("Safari 18.4 iOS", "safari184_ios"),
    "safari260": BrowserProfile("Safari 26.0", "safari260"),
    "safari260_ios": BrowserProfile("Safari 26.0 iOS", "safari260_ios"),
    "safari2601": BrowserProfile("Safari 26.0.1", "safari2601"),
    # Firefox
    "firefox133": BrowserProfile("Firefox 133", "firefox133"),
    "firefox135": BrowserProfile("Firefox 135", "firefox135"),
    "firefox144": BrowserProfile("Firefox 144", "firefox144"),
    "firefox147": BrowserProfile("Firefox 147", "firefox147"),
    # Tor
    "tor145": BrowserProfile("Tor 145", "tor145"),
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
