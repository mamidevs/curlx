"""
Tests for proxy health, host-scoped blame and the provider layer.

The existing tests/test_proxy.py pins the original behaviour - a failure retires
a proxy permanently - and is deliberately left untouched: that mode is still the
default, and if any of it broke, the new cooldown mode would have changed
semantics rather than added them.
"""

from __future__ import annotations

import time

import pytest

from curlx import (
    EndpointProvider,
    Proxy,
    ProxyPoolExhaustedError,
    ProxyRotator,
    StaticProvider,
)
from curlx.exceptions import ProxyError

P1 = "http://p1.example.com:8080"
P2 = "http://p2.example.com:8080"


class TestCooldownIsOptIn:
    def test_default_still_retires_permanently(self):
        """The pre-existing contract. Changing it silently would break callers."""
        rotator = ProxyRotator([P1, P2])
        rotator.report(P1, ok=False)
        assert rotator.count == 1
        assert rotator.available == 1

    def test_cooldown_benches_instead_of_removing(self):
        """
        Permanent retirement drains a long-running pool until the last
        invalidation turns into an outage. A benched proxy comes back on its own.
        """
        rotator = ProxyRotator([P1, P2], cooldown=60.0)
        rotator.report(P1, ok=False)
        assert rotator.count == 2  # still a member
        assert rotator.available == 1  # but not usable right now
        assert rotator.acquire().url == P2

    def test_bench_expires(self):
        rotator = ProxyRotator([P1, P2], cooldown=0.01)
        rotator.report(P1, ok=False)
        assert rotator.available == 1
        time.sleep(0.02)
        assert rotator.available == 2

    def test_cooldown_grows_with_the_failure_streak(self):
        rotator = ProxyRotator([P1, P2], cooldown=10.0, max_cooldown=40.0)
        deadlines = []
        for _ in range(4):
            rotator.report(P1, ok=False)
            deadlines.append(rotator._health[P1].cooldown_until - time.monotonic())
        # 10, 20, 40, then clamped at max_cooldown.
        assert [round(d) for d in deadlines] == [10, 20, 40, 40]

    def test_a_success_clears_the_streak(self):
        """A proxy that just worked is working; punishing it for old failures
        would keep a recovered egress benched for longer and longer."""
        rotator = ProxyRotator([P1, P2], cooldown=10.0)
        rotator.report(P1, ok=False)
        rotator.report(P1, ok=False)
        rotator.report(P1, ok=True)
        assert rotator.available == 2
        assert rotator._health[P1].failures == 0

    def test_everything_benched_refuses_a_direct_connection(self):
        """Falling back to direct would leak the IP of a caller that asked for
        a proxy - the same reason the permanent mode raises."""
        rotator = ProxyRotator([P1, P2], cooldown=60.0)
        rotator.report(P1, ok=False)
        rotator.report(P2, ok=False)
        with pytest.raises(ProxyPoolExhaustedError, match="cooldown"):
            rotator.acquire()


class TestHostScopedBlame:
    def test_a_block_benches_the_proxy_only_for_that_host(self):
        """
        A proxy one site refuses is still perfectly good everywhere else.
        Retiring it globally throws away capacity for no reason.
        """
        rotator = ProxyRotator([P1, P2], cooldown=60.0)
        rotator.report(P1, ok=False, host="blocked.example.com", blocked=True)

        assert rotator.acquire(host="blocked.example.com").url == P2
        # Untouched for any other target.
        assert rotator.available == 2
        assert {rotator.acquire(host="other.example.com").url for _ in range(4)} == {P1, P2}

    def test_a_transport_failure_benches_it_everywhere(self):
        """The hop itself did not complete; no host will fare better."""
        rotator = ProxyRotator([P1, P2], cooldown=60.0)
        rotator.report(P1, ok=False, host="a.example.com", blocked=False)
        assert rotator.acquire(host="b.example.com").url == P2

    def test_a_success_clears_only_that_host(self):
        rotator = ProxyRotator([P1, P2], cooldown=60.0)
        rotator.report(P1, ok=False, host="a.example.com", blocked=True)
        rotator.report(P1, ok=True, host="a.example.com")
        assert rotator.acquire(host="a.example.com") is not None
        assert rotator.available == 2


class TestStickiness:
    def test_the_same_key_keeps_the_same_proxy(self):
        rotator = ProxyRotator([P1, P2], sticky_sessions=True)
        first = rotator.acquire(session_key="north").url
        assert all(rotator.acquire(session_key="north").url == first for _ in range(5))

    def test_different_keys_are_not_forced_apart_but_are_stable(self):
        rotator = ProxyRotator([P1, P2], sticky_sessions=True)
        north = rotator.acquire(session_key="north").url
        south = rotator.acquire(session_key="south").url
        assert rotator.acquire(session_key="north").url == north
        assert rotator.acquire(session_key="south").url == south

    def test_health_outranks_stickiness(self):
        """A binding must never pin a session to a proxy that cannot serve it."""
        rotator = ProxyRotator([P1, P2], sticky_sessions=True, cooldown=60.0)
        bound = rotator.acquire(session_key="north")
        rotator.report(bound.url, ok=False)
        assert rotator.acquire(session_key="north").url != bound.url


class TestStaticProvider:
    @pytest.mark.asyncio
    async def test_acquire_report_refresh(self):
        provider = StaticProvider.from_urls([P1, P2], cooldown=60.0)
        proxy = await provider.acquire(key="north")
        await provider.report(proxy, ok=False)
        assert provider.rotator.available == 1
        await provider.refresh()
        assert provider.rotator.available == 2

    @pytest.mark.asyncio
    async def test_satisfies_the_protocol(self):
        from curlx import ProxyProvider

        assert isinstance(StaticProvider.from_urls([P1]), ProxyProvider)


class TestEndpointProvider:
    @staticmethod
    def _client_factory(bodies: list, calls: list):
        class FakeResp:
            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

            @property
            def text(self):
                return str(self._body)

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get(self, url):
                calls.append(url)
                if isinstance(bodies[0], Exception):
                    raise bodies[0]
                return FakeResp(bodies[min(len(calls) - 1, len(bodies) - 1)])

        return lambda: FakeClient()

    @pytest.mark.asyncio
    async def test_pool_is_fetched_and_parsed(self):
        calls: list[str] = []
        provider = EndpointProvider(
            "https://pool.example.com/lease",
            parse=lambda body: body["proxies"],
            client_factory=self._client_factory([{"proxies": [P1, P2]}], calls),
        )
        proxy = await provider.acquire()
        assert proxy.url in (P1, P2)
        assert calls == ["https://pool.example.com/lease"]

    @pytest.mark.asyncio
    async def test_pool_is_cached_until_the_ttl_lapses(self):
        calls: list[str] = []
        provider = EndpointProvider(
            "https://pool.example.com/lease",
            parse=lambda body: body["proxies"],
            ttl=60.0,
            client_factory=self._client_factory([{"proxies": [P1]}], calls),
        )
        for _ in range(5):
            await provider.acquire()
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_expired_ttl_refetches(self):
        calls: list[str] = []
        provider = EndpointProvider(
            "https://pool.example.com/lease",
            parse=lambda body: body["proxies"],
            ttl=0.0,
            client_factory=self._client_factory(
                [{"proxies": [P1]}, {"proxies": [P2]}], calls
            ),
        )
        assert (await provider.acquire()).url == P1
        assert (await provider.acquire()).url == P2
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_failed_refresh_keeps_the_previous_pool(self):
        """
        Losing the endpoint must not take the crawl down while a usable pool is
        still in hand - the alternative is an outage caused by the recovery
        mechanism itself.
        """
        calls: list[str] = []
        provider = EndpointProvider(
            "https://pool.example.com/lease",
            parse=lambda body: body["proxies"],
            ttl=0.0,
            client_factory=self._client_factory([{"proxies": [P1]}], calls),
        )
        await provider.acquire()

        provider.client_factory = self._client_factory([RuntimeError("endpoint down")], [])
        assert (await provider.acquire()).url == P1

    @pytest.mark.asyncio
    async def test_a_first_fetch_that_fails_is_an_error(self):
        """With no previous pool there is nothing to fall back to but a direct
        connection, which is exactly what must not happen silently."""
        provider = EndpointProvider(
            "https://pool.example.com/lease",
            parse=lambda body: body["proxies"],
            client_factory=self._client_factory([RuntimeError("down")], []),
        )
        with pytest.raises(ProxyError, match="Could not fetch"):
            await provider.acquire()

    @pytest.mark.asyncio
    async def test_an_empty_first_pool_is_an_error(self):
        provider = EndpointProvider(
            "https://pool.example.com/lease",
            parse=lambda body: [],
            client_factory=self._client_factory([{"proxies": []}], []),
        )
        with pytest.raises(ProxyError, match="empty"):
            await provider.acquire()

    @pytest.mark.asyncio
    async def test_parse_may_return_proxy_objects_with_weights(self):
        calls: list[str] = []
        provider = EndpointProvider(
            "https://pool.example.com/lease",
            parse=lambda body: [Proxy(url=u, weight=2.0) for u in body["proxies"]],
            client_factory=self._client_factory([{"proxies": [P1]}], calls),
        )
        assert (await provider.acquire()).weight == 2.0
