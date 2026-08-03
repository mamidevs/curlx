"""
Regression tests for the request pipeline.

Every test here pins a feature that used to be constructed and then silently
ignored: middleware hooks, the circuit breaker, proxy invalidation, and dict
proxies. If any of these stops being wired into `client.request`, one of these
tests fails rather than the feature quietly doing nothing.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from curlx import (
    AsyncHttpClient,
    MiddlewareChain,
    ProxyPoolExhaustedError,
    ProxyRotator,
    SyncHttpClient,
    TransportError,
)


class FakeRawResponse:
    """Duck-type of curl_cffi's Response, limited to what curlx reads."""

    def __init__(self, status_code: int = 200, content: bytes = b'{"ok": true}') -> None:
        self.status_code = status_code
        self.content = content
        self.url = "https://example.com/test"
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.text = content.decode()
        self.encoding = "utf-8"


def _patch_sync(client: SyncHttpClient, side_effect=None, result=None):
    mock = MagicMock(return_value=result or FakeRawResponse())
    if side_effect is not None:
        mock.side_effect = side_effect
    return patch.object(client.session, "request", mock), mock


class TestMiddlewareIsWired:
    def test_sync_request_hook_runs_and_can_mutate(self):
        seen: list[str] = []
        chain = MiddlewareChain()
        chain.add_request_hook(
            lambda cfg: (seen.append(cfg.url), replace(cfg, headers={"X-Tag": "1"}))[1]
        )
        with SyncHttpClient(middleware=chain) as client:
            ctx, mock = _patch_sync(client)
            with ctx:
                client.get("https://example.com/test")
        assert seen == ["https://example.com/test"], "request hook never fired"
        assert mock.call_args.kwargs["headers"]["X-Tag"] == "1", "hook mutation was discarded"

    def test_sync_response_hook_runs(self):
        seen: list[int] = []
        chain = MiddlewareChain()
        chain.add_response_hook(lambda resp: (seen.append(resp.status_code), resp)[1])
        with SyncHttpClient(middleware=chain) as client:
            ctx, _ = _patch_sync(client)
            with ctx:
                client.get("https://example.com/test")
        assert seen == [200], "response hook never fired"

    def test_sync_error_hook_runs_on_failure(self):
        seen: list[BaseException] = []
        chain = MiddlewareChain()
        chain.add_error_hook(lambda exc, cfg: seen.append(exc))
        with SyncHttpClient(middleware=chain) as client:
            ctx, _ = _patch_sync(client, side_effect=ConnectionError("boom"))
            with ctx, pytest.raises(TransportError):
                client.get("https://example.com/test")
        assert len(seen) == 1, "error hook never fired"

    @pytest.mark.asyncio
    async def test_async_request_hook_runs(self):
        seen: list[str] = []
        chain = MiddlewareChain()
        chain.add_request_hook(lambda cfg: (seen.append(cfg.url), cfg)[1])
        async with AsyncHttpClient(middleware=chain) as client:
            with patch.object(client.session, "request") as mock:
                mock.return_value = FakeRawResponse()
                await client.get("https://example.com/test")
        assert seen == ["https://example.com/test"]


class TestCircuitBreakerIsWired:
    def test_breaker_sees_failures_and_then_blocks(self):
        from curlx import CircuitBreaker, CircuitBreakerOpenError

        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        with SyncHttpClient(circuit_breaker=cb) as client:
            ctx, _ = _patch_sync(client, side_effect=ConnectionError("down"))
            with ctx:
                for _ in range(2):
                    with pytest.raises(TransportError):
                        client.get("https://example.com/test")
                # Third call must be short-circuited by the breaker, not attempted.
                with pytest.raises((CircuitBreakerOpenError, TransportError)) as excinfo:
                    client.get("https://example.com/test")
        assert isinstance(excinfo.value, CircuitBreakerOpenError), (
            "breaker never opened - it is not wired into the request path"
        )


class TestProxiesAreApplied:
    def test_dict_proxies_reach_the_transport(self):
        """Regression: a dict used to be stored and silently dropped (IP leak)."""
        proxies = {"http": "http://p:8080", "https": "http://p:8080"}
        with SyncHttpClient(proxies=proxies) as client:
            ctx, mock = _patch_sync(client)
            with ctx:
                client.get("https://example.com/test")
        assert mock.call_args.kwargs.get("proxies") == proxies, "dict proxies were dropped"

    def test_rotation_happens_per_request(self):
        """Regression: the rotator used to be consumed once per session."""
        rot = ProxyRotator(["http://a:1", "http://b:2"], strategy="round_robin")
        with SyncHttpClient(proxies=rot) as client:
            ctx, mock = _patch_sync(client)
            with ctx:
                for _ in range(4):
                    client.get("https://example.com/test")
        used = [c.kwargs["proxies"]["http"] for c in mock.call_args_list]
        assert used == ["http://a:1", "http://b:2", "http://a:1", "http://b:2"], used

    def test_transport_failure_invalidates_that_proxy(self):
        rot = ProxyRotator(["http://a:1", "http://b:2"])
        with SyncHttpClient(proxies=rot) as client:
            ctx, _ = _patch_sync(client, side_effect=ConnectionError("dead"))
            with ctx:
                with pytest.raises(TransportError):
                    client.get("https://example.com/test")
                assert rot.count == 1, "failed proxy was not retired"
                with pytest.raises(TransportError):
                    client.get("https://example.com/test")
                assert rot.count == 0
                # Pool is empty: must raise, never fall back to a direct connection.
                with pytest.raises(ProxyPoolExhaustedError):
                    client.get("https://example.com/test")


class TestLifecycle:
    def test_double_enter_is_rejected(self):
        client = SyncHttpClient()
        with client, pytest.raises(RuntimeError, match="already entered"):
            client.__enter__()

    def test_close_is_idempotent(self):
        client = SyncHttpClient()
        with client:
            pass
        client.close()  # must not raise

    def test_teardown_failure_does_not_mask_the_original_exception(self):
        """
        A raising `__exit__` replaces the exception the caller was unwinding.
        The user must still see their own error, not a teardown artifact.
        """
        client = SyncHttpClient()
        with pytest.raises(ValueError, match="the real problem"), client:
            client._session.close = MagicMock(side_effect=OSError("socket already gone"))
            raise ValueError("the real problem")
        assert client._session is None, "session pointer must still be cleared"

    def test_teardown_failure_alone_does_not_raise(self):
        client = SyncHttpClient()
        with client:
            client._session.close = MagicMock(side_effect=OSError("socket already gone"))
        # exiting must complete quietly


class TestCookiePersistence:
    """The README advertises cookie persistence; this proves it survives a restart."""

    def test_jar_round_trips_between_two_client_instances(self, tmp_path):
        from curlx import CookieJar

        jar = CookieJar(tmp_path / "jar.json")

        with SyncHttpClient(cookie_jar=jar) as first:
            first.session.cookies.update({"sid": "abc123"})

        assert jar.load() == {"sid": "abc123"}, "cookies were not written on close"

        with SyncHttpClient(cookie_jar=CookieJar(tmp_path / "jar.json")) as second:
            assert dict(second.session.cookies).get("sid") == "abc123", (
                "a fresh client did not restore the persisted jar"
            )

    def test_session_property_guards_unentered_use(self):
        client = SyncHttpClient()
        with pytest.raises(RuntimeError, match="not entered"):
            _ = client.session

    @pytest.mark.asyncio
    async def test_async_double_enter_is_rejected(self):
        client = AsyncHttpClient()
        async with client:
            with pytest.raises(RuntimeError, match="already entered"):
                await client.__aenter__()


class TestRequestKwargs:
    """The old suite only asserted `called_once`, so kwarg drift shipped green."""

    def test_body_and_option_kwargs_are_forwarded(self):
        with SyncHttpClient(timeout=7.5, verify=False) as client:
            ctx, mock = _patch_sync(client)
            with ctx:
                client.post(
                    "https://example.com/test",
                    json={"a": 1},
                    params={"q": "x"},
                    cookies={"c": "1"},
                )
        kwargs = mock.call_args.kwargs
        assert mock.call_args.args[0] == "POST", "method must be upper-cased on the wire"
        assert kwargs["json"] == {"a": 1}
        assert kwargs["params"] == {"q": "x"}
        assert kwargs["cookies"] == {"c": "1"}
        assert kwargs["timeout"] == 7.5
        assert kwargs["verify"] is False

    def test_lowercase_method_is_normalized(self):
        with SyncHttpClient() as client:
            ctx, mock = _patch_sync(client)
            with ctx:
                client.request("get", "https://example.com/test")
        assert mock.call_args.args[0] == "GET"

    def test_unknown_kwargs_pass_through_to_curl_cffi(self):
        with SyncHttpClient() as client:
            ctx, mock = _patch_sync(client)
            with ctx:
                client.get("https://example.com/test", stream=False, referer="https://ref")
        assert mock.call_args.kwargs["referer"] == "https://ref"

    def test_safe_redirects_is_the_default(self):
        with SyncHttpClient() as client:
            ctx, mock = _patch_sync(client)
            with ctx:
                client.get("https://example.com/test")
        assert mock.call_args.kwargs["allow_redirects"] == "safe"
