"""
Browser fingerprint presets for curl_cffi impersonation.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import get_args

from curlx.exceptions import ProfileNotFoundError


@dataclass(frozen=True)
class BrowserProfile:
    """
    A complete browser fingerprint profile.

    ``impersonate`` is the token handed to curl_cffi verbatim and is always a
    member of ``curl_cffi.requests.impersonate.BrowserTypeLiteral``.  ``ja3`` and
    ``akamai`` override the TLS / HTTP2 fingerprint of that target when set.

    ``alias_of`` names the concrete target curl_cffi resolves this key to, and is
    ``None`` for the real targets.  It is set for both kinds of alias curl_cffi
    accepts: the *latest* aliases (``chrome`` -> ``chrome146``), which are worth
    passing through unchanged so curlx tracks the newest target across curl_cffi
    upgrades, and the *deprecated* spellings (``safari18_0`` -> ``safari180``),
    which are flagged with ``deprecated`` so callers can hide them.
    """

    name: str
    impersonate: str
    ja3: str | None = None
    akamai: str | None = None
    alias_of: str | None = None
    deprecated: bool = False


# Real impersonation targets: one entry per distinct fingerprint.
_CANONICAL_TARGETS: dict[str, str] = {
    # Chrome
    "chrome99": "Chrome 99",
    "chrome100": "Chrome 100",
    "chrome101": "Chrome 101",
    "chrome104": "Chrome 104",
    "chrome107": "Chrome 107",
    "chrome110": "Chrome 110",
    "chrome116": "Chrome 116",
    "chrome119": "Chrome 119",
    "chrome120": "Chrome 120",
    "chrome123": "Chrome 123",
    "chrome124": "Chrome 124",
    "chrome131": "Chrome 131",
    "chrome133a": "Chrome 133a",
    "chrome136": "Chrome 136",
    "chrome142": "Chrome 142",
    "chrome145": "Chrome 145",
    "chrome146": "Chrome 146",
    "chrome99_android": "Chrome 99 Android",
    "chrome131_android": "Chrome 131 Android",
    # Edge - curl_cffi ships nothing newer than Chromium 101 here.
    "edge99": "Edge 99",
    "edge101": "Edge 101",
    # Safari
    "safari153": "Safari 15.3",
    "safari155": "Safari 15.5",
    "safari170": "Safari 17.0",
    "safari172_ios": "Safari 17.2 iOS",
    "safari180": "Safari 18.0",
    "safari180_ios": "Safari 18.0 iOS",
    "safari184": "Safari 18.4",
    "safari184_ios": "Safari 18.4 iOS",
    "safari260": "Safari 26.0",
    "safari260_ios": "Safari 26.0 iOS",
    "safari2601": "Safari 26.0.1",
    # Firefox
    "firefox133": "Firefox 133",
    "firefox135": "Firefox 135",
    "firefox144": "Firefox 144",
    "firefox147": "Firefox 147",
    # Tor (Firefox-based)
    "tor145": "Tor 145",
}

# Aliases curl_cffi resolves to whatever was newest at *its* release, mirroring
# ``curl_cffi.requests.impersonate.REAL_TARGET_MAP``.  The target shown here is
# only documentation - the alias itself is what gets sent, so upgrading
# curl_cffi silently moves these forward.
#
# ``REAL_TARGET_MAP`` also carries a ``tor`` entry, but it is absent from
# ``BrowserTypeLiteral``, so it is deliberately not exposed; use ``tor145``.
_LATEST_ALIASES: dict[str, tuple[str, str]] = {
    "chrome": ("Chrome (latest)", "chrome146"),
    "chrome_android": ("Chrome Android (latest)", "chrome131_android"),
    "edge": ("Edge (latest)", "edge101"),
    "safari": ("Safari (latest)", "safari2601"),
    "safari_ios": ("Safari iOS (latest)", "safari260_ios"),
    "safari_beta": ("Safari Beta (latest)", "safari2601"),
    "safari_ios_beta": ("Safari iOS Beta (latest)", "safari260_ios"),
    "firefox": ("Firefox (latest)", "firefox147"),
}

# Older spellings curl_cffi still accepts but marks deprecated.  These are not
# separate fingerprints - each is byte-identical to its canonical target - so
# their display name is derived rather than duplicated.
_DEPRECATED_ALIASES: dict[str, str] = {
    "safari15_3": "safari153",
    "safari15_5": "safari155",
    "safari17_0": "safari170",
    "safari17_2_ios": "safari172_ios",
    "safari18_0": "safari180",
    "safari18_0_ios": "safari180_ios",
    "safari18_4": "safari184",
    "safari18_4_ios": "safari184_ios",
}


def _build_supported_profiles() -> dict[str, BrowserProfile]:
    profiles = {key: BrowserProfile(name, key) for key, name in _CANONICAL_TARGETS.items()}
    for key, (name, target) in _LATEST_ALIASES.items():
        profiles[key] = BrowserProfile(name, key, alias_of=target)
    for key, target in _DEPRECATED_ALIASES.items():
        base = profiles[target]
        profiles[key] = BrowserProfile(
            f"{base.name} (deprecated alias of {target})",
            key,
            alias_of=target,
            deprecated=True,
        )
    return profiles


# Every key here is a member of ``BrowserTypeLiteral``; see
# :func:`verify_supported_profiles`.  ``BrowserType`` is *not* the source of
# truth - curl_cffi 0.16 marks that enum "TODO: remove in version 1.x" and it
# already lags the Literal by two entries.
SUPPORTED_PROFILES: dict[str, BrowserProfile] = _build_supported_profiles()


def _unknown_profile_message(name: object) -> str:
    hint = ""
    if isinstance(name, str):
        close = difflib.get_close_matches(name, SUPPORTED_PROFILES, n=3, cutoff=0.5)
        if close:
            hint = f" Did you mean: {', '.join(close)}?"
    return (
        f"Unknown impersonate profile: {name!r}.{hint}"
        f" {len(SUPPORTED_PROFILES)} profiles are supported -"
        " call curlx.fingerprint.list_profiles() for the full list."
    )


def get_profile(name: str) -> BrowserProfile:
    """
    Get a browser profile by name.

    Raises :class:`~curlx.exceptions.ProfileNotFoundError` for unknown names,
    suggesting close matches rather than dumping the whole table.
    """
    profile = SUPPORTED_PROFILES.get(name)
    if profile is None:
        raise ProfileNotFoundError(_unknown_profile_message(name))
    return profile


def list_profiles(*, include_deprecated: bool = True) -> list[str]:
    """
    Return all supported impersonate profile names.

    Deprecated spellings are included by default because they remain valid input
    to :func:`get_profile`.  Pass ``include_deprecated=False`` when rendering a
    list for humans, so the aliased duplicates do not show up as extra rows.
    """
    return [
        name
        for name, profile in SUPPORTED_PROFILES.items()
        if include_deprecated or not profile.deprecated
    ]


def verify_supported_profiles() -> tuple[list[str], list[str], list[str]]:
    """
    Diff :data:`SUPPORTED_PROFILES` against the installed curl_cffi.

    Returns ``(unsupported, missing, stale)``: keys curlx offers that curl_cffi
    would reject, targets curl_cffi accepts that curlx does not expose, and
    latest-aliases whose recorded ``alias_of`` no longer matches what curl_cffi
    resolves them to.  All three are empty when the table is in sync, which is
    the invariant this module claims.
    """
    # Imported lazily so importing curlx never depends on a curl_cffi internal.
    from curl_cffi.requests import impersonate as cc

    accepted = set(get_args(cc.BrowserTypeLiteral))
    ours = set(SUPPORTED_PROFILES)

    # REAL_TARGET_MAP is internal; if it ever disappears, report no drift rather
    # than failing the membership check that matters more.
    resolved: dict[str, str] = getattr(cc, "REAL_TARGET_MAP", {})
    stale = [
        f"{key}: recorded {target!r}, curl_cffi resolves to {resolved[key]!r}"
        for key, (_, target) in _LATEST_ALIASES.items()
        if key in resolved and resolved[key] != target
    ]
    return sorted(ours - accepted), sorted(accepted - ours), stale
