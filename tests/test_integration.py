"""
Integration tests: real curl_cffi, real sockets, real network.

The rest of the suite fakes curl_cffi with hand-written duck types, which is
exactly why the 0.14 `elapsed` float->timedelta change could have shipped green.
These tests exist so an upstream API break fails loudly instead.

Run with:   pytest -m integration
Skip with:  pytest -m 'not integration'
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import pytest

from curlx import (
    AsyncHttpClient,
    ProxyPoolExhaustedError,
    ProxyRotator,
    SyncHttpClient,
    TransportError,
)

pytestmark = pytest.mark.integration

HTTPBIN = "https://httpbin.org"
# A port nothing listens on, for deterministic connection failures.
DEAD_PROXY = "http://127.0.0.1:9"


@pytest.fixture(scope="module")
def _network() -> None:
    """Skip the whole module when the network or httpbin is unavailable."""
    try:
        with SyncHttpClient(timeout=15) as client:
            client.get(f"{HTTPBIN}/status/200")
    except Exception as exc:
        pytest.skip(f"network/httpbin unavailable: {exc}")


class TestRealTransport:
    def test_sync_get(self, _network):
        with SyncHttpClient(impersonate="chrome", timeout=30) as client:
            resp = client.get(f"{HTTPBIN}/get")
        assert resp.status_code == 200
        assert resp.json()["url"].endswith("/get")

    def test_elapsed_is_a_timedelta(self, _network):
        """curl_cffi 0.14 changed this from float; a fake response would not catch it."""
        with SyncHttpClient(timeout=30) as client:
            resp = client.get(f"{HTTPBIN}/get")
        assert isinstance(resp.elapsed, timedelta)
        assert resp.elapsed.total_seconds() > 0

    def test_headers_are_case_insensitive(self, _network):
        """Real Headers is a multi-dict, not the plain dict the fakes use."""
        with SyncHttpClient(timeout=30) as client:
            resp = client.get(f"{HTTPBIN}/get")
        assert resp.headers.get("CONTENT-TYPE") == resp.headers.get("content-type")

    def test_post_json_roundtrip(self, _network):
        with SyncHttpClient(timeout=30) as client:
            resp = client.post(f"{HTTPBIN}/post", json={"hello": "world"})
        assert resp.json()["json"] == {"hello": "world"}

    def test_raise_for_status_surfaces_http_errors(self, _network):
        from curlx import HttpStatusError

        with (
            SyncHttpClient(timeout=30, raise_for_status=True) as client,
            pytest.raises(HttpStatusError) as excinfo,
        ):
            client.get(f"{HTTPBIN}/status/503")
        assert excinfo.value.status_code == 503

    def test_cookies_round_trip_through_the_session(self, _network):
        with SyncHttpClient(timeout=30) as client:
            client.get(f"{HTTPBIN}/cookies/set/sid/abc123")
            resp = client.get(f"{HTTPBIN}/cookies")
        assert resp.json()["cookies"].get("sid") == "abc123"

    @pytest.mark.asyncio
    async def test_async_fetch_all(self, _network):
        async with AsyncHttpClient(timeout=30, max_concurrent=4) as client:
            results = await client.fetch_all([f"{HTTPBIN}/get", f"{HTTPBIN}/ip"])
        assert [r.status_code for r in results] == [200, 200]

    @pytest.mark.asyncio
    async def test_rate_limiter_paces_real_requests(self, _network):
        started = asyncio.get_running_loop().time()
        async with AsyncHttpClient(timeout=30, requests_per_second=2) as client:
            await client.fetch_all([f"{HTTPBIN}/get"] * 3)
        # 3 requests at 2/s must span at least one refill interval.
        assert asyncio.get_running_loop().time() - started >= 0.5


class TestProxyEnforcement:
    """The pre-refactor client silently ignored proxies, leaking the real IP."""

    def test_dict_proxy_is_enforced_not_bypassed(self, _network):
        with (
            SyncHttpClient(proxies={"http": DEAD_PROXY, "https": DEAD_PROXY}, timeout=15) as client,
            pytest.raises(TransportError),
        ):
            client.get(f"{HTTPBIN}/ip")

    def test_exhausted_pool_refuses_to_go_direct(self, _network):
        rotator = ProxyRotator([DEAD_PROXY])
        with SyncHttpClient(proxies=rotator, timeout=15) as client:
            with pytest.raises(TransportError):
                client.get(f"{HTTPBIN}/ip")
            assert rotator.count == 0, "dead proxy should have been retired"
            with pytest.raises(ProxyPoolExhaustedError):
                client.get(f"{HTTPBIN}/ip")


class TestFingerprintCoherence:
    """
    The headline defect: TLS said one Chrome, the headers said another.

    curl_cffi applies the impersonated TLS/HTTP2 fingerprint; curlx must not
    contradict it with a stale hardcoded User-Agent.
    """

    def test_user_agent_matches_the_impersonated_target(self, _network):
        from curl_cffi.requests import impersonate as imp

        with SyncHttpClient(impersonate="chrome", timeout=30) as client:
            sent_ua = client.get(f"{HTTPBIN}/get").json()["headers"]["User-Agent"]

        match = re.search(r"Chrome/(\d+)", sent_ua)
        assert match, f"no Chrome version in UA: {sent_ua}"
        assert match.group(1) in imp.DEFAULT_CHROME, (
            f"UA claims Chrome {match.group(1)} but curl_cffi impersonates "
            f"{imp.DEFAULT_CHROME} - fingerprint and headers disagree"
        )

    def test_header_builder_does_not_override_the_impersonated_ua(self, _network):
        from curlx import HeaderBuilder

        headers = HeaderBuilder().with_browser("chrome").build()
        assert "User-Agent" not in headers

        with SyncHttpClient(impersonate="chrome", default_headers=headers, timeout=30) as client:
            sent = client.get(f"{HTTPBIN}/get").json()["headers"]
        assert "Chrome/" in sent["User-Agent"], "impersonation UA was lost"
        assert "zstd" in sent.get("Accept-Encoding", "")
        assert "Dnt" not in sent and "DNT" not in sent, "Chrome no longer sends DNT"
