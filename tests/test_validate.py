"""
Tests for the ``validate`` hook - turning a structurally fine 200 into a failure.

Each test here pins one decision from the design, and the two most important
ones are negative: a rejection must **not** trip the circuit breaker, and must
**not** retire the proxy unless the caller says the egress is to blame. Both
were deliberate, both are easy to "fix" into the opposite, and both have a
concrete failure mode if that happens.
"""

from __future__ import annotations

import pytest

from curlx import (
    AsyncHttpClient,
    CircuitBreaker,
    IdentityStaleError,
    MiddlewareChain,
    ProxyRotator,
    ResponseInvalidError,
    RetryableResponseError,
    SyncHttpClient,
)


def _reject(detail: str = "empty body", **kwargs):
    def validate(resp):
        raise ResponseInvalidError("rejected", detail=detail, response=resp, **kwargs)

    return validate


class TestValidateFires:
    @pytest.mark.asyncio
    async def test_rejection_surfaces_to_the_caller(self, patch_async):
        async with AsyncHttpClient(validate=_reject()) as client:
            patcher, _ = patch_async(client)
            with patcher, pytest.raises(ResponseInvalidError) as exc:
                await client.get("https://example.com/test")
        assert exc.value.detail == "empty body"
        # The response rides along so a handler can inspect the body without
        # re-issuing the request.
        assert exc.value.response is not None
        assert exc.value.response.status_code == 200

    def test_sync_client_validates_too(self, patch_sync):
        with SyncHttpClient(validate=_reject()) as client:
            patcher, _ = patch_sync(client)
            with patcher, pytest.raises(ResponseInvalidError):
                client.get("https://example.com/test")

    @pytest.mark.asyncio
    async def test_none_leaves_behaviour_untouched(self, patch_async):
        async with AsyncHttpClient() as client:
            patcher, _ = patch_async(client)
            with patcher:
                resp = await client.get("https://example.com/test")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_validator_that_accepts_does_not_interfere(self, patch_async):
        seen = []

        async with AsyncHttpClient(validate=seen.append) as client:
            patcher, _ = patch_async(client)
            with patcher:
                resp = await client.get("https://example.com/test")
        assert resp.status_code == 200
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_error_hooks_observe_the_rejection(self, patch_async):
        """A rejection must travel the same error path a transport failure does."""
        seen: list[Exception] = []
        chain = MiddlewareChain().add_error_hook(lambda exc, cfg: seen.append(exc))

        async with AsyncHttpClient(validate=_reject(), middleware=chain) as client:
            patcher, _ = patch_async(client)
            with patcher, pytest.raises(ResponseInvalidError):
                await client.get("https://example.com/test")
        assert len(seen) == 1
        assert isinstance(seen[0], ResponseInvalidError)


class TestValidateAndCircuitBreaker:
    @pytest.mark.asyncio
    async def test_rejections_do_not_trip_the_breaker(self, patch_async):
        """
        The deadlock this prevents: if rejections counted as breaker failures,
        a stale identity would open the breaker, and the warm-up request that
        would fix the identity would then be refused by the open breaker. The
        session could never recover.
        """
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        async with AsyncHttpClient(validate=_reject(), circuit_breaker=breaker) as client:
            patcher, _ = patch_async(client)
            with patcher:
                for _ in range(5):
                    with pytest.raises(ResponseInvalidError):
                        await client.get("https://example.com/test")

        assert breaker.state == "CLOSED"
        assert breaker.failure_count == 0


class TestValidateAndProxyHealth:
    @staticmethod
    def _rotator() -> ProxyRotator:
        return ProxyRotator(["http://p1.example.com:8080", "http://p2.example.com:8080"])

    @pytest.mark.asyncio
    async def test_plain_rejection_keeps_the_proxy(self, patch_async):
        """A lying backend is not a broken egress; the pool must not shrink."""
        rotator = self._rotator()

        async with AsyncHttpClient(validate=_reject(), proxies=rotator) as client:
            patcher, _ = patch_async(client)
            with patcher, pytest.raises(ResponseInvalidError):
                await client.get("https://example.com/test")

        assert rotator.count == 2

    @pytest.mark.asyncio
    async def test_blaming_the_proxy_retires_it(self, patch_async):
        """
        A block page arrives as a healthy 200 over a healthy connection, so
        nothing below the caller can recognise it. blames_proxy is how that
        knowledge reaches the pool.
        """
        rotator = self._rotator()

        async with AsyncHttpClient(
            validate=_reject("blocked", blames_proxy=True), proxies=rotator
        ) as client:
            patcher, _ = patch_async(client)
            with patcher, pytest.raises(ResponseInvalidError):
                await client.get("https://example.com/test")

        assert rotator.count == 1


class TestExceptionTaxonomy:
    def test_only_the_retryable_variant_is_in_the_default_tuple(self):
        """
        with_retry can only re-issue the same request. Re-issuing it against a
        stale or spent identity cannot help, so those two must stay out of the
        default retryable set - otherwise a caller combining @with_retry with
        validate= burns its attempt budget and reports a network failure for
        what is actually a binding problem.
        """
        from curlx.retry import DEFAULT_RETRYABLE_EXCEPTIONS, is_retryable

        assert RetryableResponseError in DEFAULT_RETRYABLE_EXCEPTIONS
        assert is_retryable(RetryableResponseError("x"))
        assert not is_retryable(IdentityStaleError("x"))
        assert not is_retryable(ResponseInvalidError("x"))

    def test_identity_errors_are_siblings_not_children(self):
        """
        Sibling, not subclass: inheriting from RetryableResponseError would put
        them back in the default tuple through the isinstance check, silently
        undoing the test above.
        """
        assert not issubclass(IdentityStaleError, RetryableResponseError)
        assert issubclass(IdentityStaleError, ResponseInvalidError)

    def test_detail_is_visible_in_the_message(self):
        exc = ResponseInvalidError("rejected", detail="storeId mismatch")
        assert "storeId mismatch" in str(exc)
        assert str(ResponseInvalidError("rejected")) == "rejected"


class TestSessionKeyIsNotForwarded:
    @pytest.mark.asyncio
    async def test_session_key_never_reaches_curl_cffi(self, patch_async, fake_response):
        """
        It is popped before RequestConfig is built. Left in, it would land in
        `extra` and be forwarded to curl_cffi, which has never heard of it.
        """
        async with AsyncHttpClient() as client:
            patcher, mock = patch_async(client, result=fake_response())
            with patcher:
                await client.get("https://example.com/test", session_key="north")
        _, kwargs = mock.call_args
        assert "session_key" not in kwargs
