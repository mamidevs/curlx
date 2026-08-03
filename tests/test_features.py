"""
Tests for the modules that backed README claims which used to be vapor:
token-bucket rate limiting, persistent cookie jars, fingerprint coherence and
the richer Response surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import timedelta

import pytest

from curlx import (
    CookieJar,
    HeaderBuilder,
    ProfileNotFoundError,
    RateLimiter,
    Response,
    get_profile,
    list_profiles,
)
from curlx.models import parse_retry_after


class TestRateLimiter:
    def test_rejects_non_positive_rate(self):
        with pytest.raises(ValueError):
            RateLimiter(0)
        with pytest.raises(ValueError):
            RateLimiter(-1)

    def test_burst_is_immediate_then_throttles(self):
        rl = RateLimiter(20, burst=3)
        start = time.monotonic()
        for _ in range(3):
            rl.acquire()
        assert time.monotonic() - start < 0.05, "burst should not sleep"

        rl.acquire()
        assert time.monotonic() - start >= 0.04, "4th token must wait ~1/20s"

    @pytest.mark.asyncio
    async def test_async_acquire_throttles(self):
        rl = RateLimiter(20, burst=1)
        start = time.monotonic()
        for _ in range(3):
            await rl.acquire_async()
        assert time.monotonic() - start >= 0.08

    @pytest.mark.asyncio
    async def test_concurrent_tasks_are_not_serialized_by_the_lock(self):
        """A lock held across the sleep would make this take ~1s instead of ~0."""
        rl = RateLimiter(1000, burst=50)
        start = time.monotonic()
        await asyncio.gather(*(rl.acquire_async() for _ in range(50)))
        assert time.monotonic() - start < 0.3

    def test_per_host_buckets_are_independent(self):
        rl = RateLimiter(20, burst=1, per_host=True)
        rl.acquire("a.example")
        start = time.monotonic()
        rl.acquire("b.example")  # different bucket -> no wait
        assert time.monotonic() - start < 0.03


class TestCookieJar:
    def test_missing_file_loads_empty(self, tmp_path):
        jar = CookieJar(tmp_path / "nope" / "jar.json")
        assert jar.load() == {}

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "sub" / "jar.json"
        CookieJar(path).save({"sid": "abc", "csrf": "xyz"})
        assert CookieJar(path).load() == {"sid": "abc", "csrf": "xyz"}

    def test_file_is_owner_only(self, tmp_path):
        path = tmp_path / "jar.json"
        CookieJar(path).save({"a": "1"})
        assert oct(os.stat(path).st_mode & 0o777) == "0o600", "cookies are credentials"

    def test_corrupt_file_degrades_to_empty(self, tmp_path):
        path = tmp_path / "jar.json"
        path.write_text("{not json at all")
        assert CookieJar(path).load() == {}, "a corrupt jar must not crash startup"

    def test_clear_removes_file(self, tmp_path):
        path = tmp_path / "jar.json"
        jar = CookieJar(path)
        jar.save({"a": "1"})
        jar.clear()
        assert not path.exists()
        assert jar.load() == {}

    def test_save_leaves_no_temp_files_behind(self, tmp_path):
        path = tmp_path / "jar.json"
        jar = CookieJar(path)
        jar.save({"a": "1"})
        jar.save({"b": "2"})
        assert json.loads(path.read_text()) is not None
        assert [p.name for p in tmp_path.iterdir()] == ["jar.json"]

    def test_netscape_format_roundtrip(self, tmp_path):
        path = tmp_path / "cookies.txt"
        jar = CookieJar(path, fmt="netscape")
        jar.save({"sid": "abc"})
        assert path.read_text().startswith("# Netscape HTTP Cookie File")
        assert CookieJar(path, fmt="netscape").load() == {"sid": "abc"}


class TestFingerprintCatalogue:
    def test_aliases_are_accepted(self):
        """`impersonate="chrome"` used to be rejected by curlx alone."""
        for alias in ("chrome", "firefox", "safari", "edge", "chrome_android"):
            assert get_profile(alias).impersonate

    def test_unknown_profile_raises_typed_error_not_valueerror(self):
        with pytest.raises(ProfileNotFoundError):
            get_profile("netscape_navigator_3")

    def test_every_profile_is_known_to_curl_cffi(self):
        """The local whitelist must never advertise a target curl_cffi rejects."""
        import typing

        from curl_cffi.requests import impersonate as imp

        supported = set(typing.get_args(imp.BrowserTypeLiteral))
        offered = {get_profile(name).impersonate for name in list_profiles()}
        assert offered <= supported, f"unknown to curl_cffi: {sorted(offered - supported)}"


class TestHeaderBuilderCoherence:
    def test_builder_is_immutable(self):
        base = HeaderBuilder()
        derived = base.with_browser("firefox")
        assert base is not derived

    def test_no_user_agent_by_default(self):
        """
        Overriding the UA contradicts curl_cffi's impersonation-matched header,
        which is precisely the mismatch WAFs look for.
        """
        assert "User-Agent" not in HeaderBuilder().build()

    def test_explicit_user_agent_is_honored(self):
        headers = HeaderBuilder().with_user_agent("Custom/1.0").build()
        assert headers["User-Agent"] == "Custom/1.0"

    def test_modern_accept_encoding_includes_zstd(self):
        assert "zstd" in HeaderBuilder().build()["Accept-Encoding"]

    def test_dnt_is_not_sent(self):
        assert "DNT" not in HeaderBuilder().build(), "Chrome dropped DNT; sending it is a tell"

    def test_client_hints_agree_with_the_user_agent(self):
        headers = (
            HeaderBuilder()
            .with_browser("chrome")
            .with_user_agent(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            )
            .build()
        )
        if "sec-ch-ua" in headers:
            assert "146" in headers["sec-ch-ua"], headers["sec-ch-ua"]
        if "sec-ch-ua-platform" in headers:
            assert "mac" in headers["sec-ch-ua-platform"].lower()

    def test_custom_headers_win(self):
        headers = HeaderBuilder().with_header("X-Mine", "1").build()
        assert headers["X-Mine"] == "1"


class _Raw:
    """Minimal stand-in for curl_cffi's Response."""

    def __init__(self, **kw):
        self.status_code = kw.get("status_code", 200)
        self.content = kw.get("content", b'{"ok":true}')
        self.text = self.content.decode()
        self.url = "https://example.com"
        self.headers = kw.get("headers", {})
        self.cookies = {}
        self.encoding = "utf-8"
        for k, v in kw.items():
            if k not in {"status_code", "content", "headers"}:
                setattr(self, k, v)


class TestResponseSurface:
    def test_elapsed_float_is_normalized_to_timedelta(self):
        """curl_cffi 0.14 changed elapsed from float to timedelta."""
        assert Response(_Raw(elapsed=1.5)).elapsed == timedelta(seconds=1.5)
        assert Response(_Raw(elapsed=timedelta(seconds=2))).elapsed == timedelta(seconds=2)
        assert Response(_Raw()).elapsed is None

    def test_status_helpers(self):
        assert Response(_Raw(status_code=204)).is_success()
        assert Response(_Raw(status_code=302)).is_redirect()
        assert Response(_Raw(status_code=404)).is_client_error()
        assert Response(_Raw(status_code=503)).is_server_error()

    def test_raise_for_status_carries_retry_after(self):
        from curlx import HttpStatusError

        resp = Response(_Raw(status_code=429, headers={"retry-after": "7"}))
        with pytest.raises(HttpStatusError) as excinfo:
            resp.raise_for_status()
        assert excinfo.value.status_code == 429
        assert excinfo.value.retry_after == 7.0

    def test_raise_for_status_is_silent_on_success(self):
        Response(_Raw(status_code=200)).raise_for_status()

    def test_json_uses_negotiated_charset(self):
        raw = _Raw(content='{"v":"ünïcode"}'.encode())
        assert Response(raw).json()["v"] == "ünïcode"

    def test_extra_fields_default_safely(self):
        resp = Response(_Raw())
        assert resp.redirect_count == 0
        assert resp.history == []
        assert resp.primary_ip is None
        assert resp.response_size is None
        assert resp.reason == ""

    def test_repr_is_informative(self):
        assert "200" in repr(Response(_Raw()))


class TestParseRetryAfter:
    def test_seconds(self):
        assert parse_retry_after("120") == 120.0

    def test_http_date_is_relative_to_now(self):
        import email.utils

        future = email.utils.formatdate(time.time() + 60, usegmt=True)
        value = parse_retry_after(future)
        assert value is not None and 50 < value <= 61

    def test_past_date_clamps_to_zero(self):
        import email.utils

        past = email.utils.formatdate(time.time() - 500, usegmt=True)
        assert parse_retry_after(past) == 0.0

    def test_garbage_and_empty(self):
        assert parse_retry_after("later please") is None
        assert parse_retry_after(None) is None
        assert parse_retry_after("") is None
