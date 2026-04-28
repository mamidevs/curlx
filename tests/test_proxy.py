"""
Tests for proxy rotation.
"""

import pytest

from curlx.exceptions import ProxyError
from curlx.proxy import Proxy, ProxyRotator


class TestProxyRotator:
    def test_round_robin(self):
        rot = ProxyRotator(["http://p1:8080", "http://p2:8080"], strategy="round_robin")
        p1 = rot.get()
        p2 = rot.get()
        p3 = rot.get()
        assert p1 == {"http": "http://p1:8080", "https": "http://p1:8080"}
        assert p2 == {"http": "http://p2:8080", "https": "http://p2:8080"}
        assert p3 == p1  # cycles back

    def test_random(self):
        rot = ProxyRotator(["http://p1:8080", "http://p2:8080"], strategy="random")
        p = rot.get()
        assert p is not None
        assert "http" in p

    def test_weighted_random(self):
        rot = ProxyRotator(
            [Proxy("http://p1:8080", weight=1), Proxy("http://p2:8080", weight=99)],
            strategy="weighted_random",
        )
        # Just ensure it returns something
        assert rot.get() is not None

    def test_sticky_sessions(self):
        rot = ProxyRotator(
            ["http://p1:8080", "http://p2:8080"],
            strategy="round_robin",
            sticky_sessions=True,
        )
        s1 = rot.get(session_key="session-1")
        s1_again = rot.get(session_key="session-1")
        assert s1 == s1_again

    def test_empty_proxies_raises(self):
        with pytest.raises(ProxyError):
            ProxyRotator([])

    def test_invalidate(self):
        rot = ProxyRotator(["http://p1:8080", "http://p2:8080"])
        rot.invalidate("http://p1:8080")
        assert rot.count == 1
        for _ in range(5):
            assert rot.get() == {"http": "http://p2:8080", "https": "http://p2:8080"}
