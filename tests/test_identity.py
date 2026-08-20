"""
Tests for bound sessions, quota gates and identity pools.

These pin behaviours that were each paid for once already in production
crawlers: the order of operations in a recycle, single-flight repair, and the
rule that a spent budget does not consume a retry attempt. All three look like
harmless implementation details and all three cause silent data loss when they
regress, which is exactly why they are asserted rather than trusted.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from curlx import Identity, IdentityPool, QuotaGate, RateLimiter
from curlx.exceptions import (
    ConnectionFailedError,
    IdentityExhaustedError,
    IdentityStaleError,
    ResponseInvalidError,
    RetryExhaustedError,
)
from curlx.identity import BoundSession


class FakeClient:
    """Minimal stand-in for AsyncHttpClient: enter, request, close."""

    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, responses=None):
        self.entered = False
        self.closed = False
        self.calls: list[tuple[str, str, dict]] = []
        self.warmed = 0
        # Shared, not copied: a recycle must continue the script, not restart
        # it. Copying here made every new client replay the same failures and
        # turned the retry loop into an infinite one.
        self._responses = responses if responses is not None else []
        FakeClient.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def aclose(self):
        self.closed = True

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        # A real request suspends. Without a yield here every worker runs to
        # completion before the next one starts, single-flight logic never sees
        # concurrency, and these tests would assert the wrong thing.
        await asyncio.sleep(0)
        if self._responses:
            outcome = self._responses.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return f"{method} {url}"


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeClient.instances.clear()
    yield
    FakeClient.instances.clear()


def make_session(responses=None, warmup=None, **kwargs) -> BoundSession:
    queue = list(responses or [])

    def factory(identity):
        # Each client gets the remaining outcomes, so a recycle continues the
        # script rather than restarting it.
        return FakeClient(queue)

    return BoundSession(Identity("north", payload=1), client_factory=factory,
                        warmup=warmup, **kwargs)


class TestRecycleOrder:
    @pytest.mark.asyncio
    async def test_new_client_is_warmed_before_it_becomes_current(self):
        """
        The invariant: while the replacement is being warmed, session.client is
        still the old one. Swapping first leaves a window - a few hundred ms is
        enough - in which workers hit an unwarmed session. Those responses come
        back wrong, which reads as drift, which triggers another recycle. A
        production crawler recycled 60 times in 30 seconds on this exact bug.
        """
        observed: list[bool] = []
        session = make_session()

        async def warmup(client, identity):
            # True only if the client being warmed is already the live one,
            # i.e. the swap happened too early.
            observed.append(client is session.client)
            client.warmed += 1

        session.warmup = warmup

        async with session:
            first = session.client
            await session.recycle()
            assert session.client is not first

        # Entry warms the very first client, which is not yet live either.
        assert observed == [False, False]

    @pytest.mark.asyncio
    async def test_old_client_is_closed_after_the_swap(self):
        session = make_session()
        async with session:
            first = session.client
            await session.recycle()
        assert first.closed
        assert session.stats.recycles == 1

    @pytest.mark.asyncio
    async def test_failed_warmup_does_not_replace_a_working_client(self):
        calls = {"n": 0}

        async def warmup(client, identity):
            calls["n"] += 1
            if calls["n"] > 1:  # the entry warm-up succeeds, the recycle fails
                raise ConnectionFailedError("warm-up failed")

        session = make_session(warmup=warmup)
        async with session:
            good = session.client
            with pytest.raises(ConnectionFailedError):
                await session.recycle()
            # The session is still usable on the client that already worked.
            assert session.client is good
            assert not good.closed


class TestSingleFlightRepair:
    @pytest.mark.asyncio
    async def test_concurrent_drift_triggers_exactly_one_rewarm(self):
        """
        Ten workers noticing the same stale binding must produce one warm-up,
        not ten. Ten would each re-bind the session, and the last one wins by
        accident.
        """
        warmups = {"n": 0}

        async def warmup(client, identity):
            warmups["n"] += 1
            await asyncio.sleep(0.01)  # widen the race window

        stale = IdentityStaleError("drifted")
        # Every worker gets one stale response, then a good one.
        session = make_session(responses=[stale] * 10 + ["ok"] * 10, warmup=warmup)

        async with session:
            results = await asyncio.gather(
                *(session.fetch("GET", "https://example.com/x") for _ in range(10))
            )

        assert results == ["ok"] * 10
        # 1 at entry + exactly 1 repair for the whole burst.
        assert warmups["n"] == 2
        assert session.stats.rewarms == 1

    @pytest.mark.asyncio
    async def test_concurrent_exhaustion_triggers_exactly_one_recycle(self):
        spent = IdentityExhaustedError("budget spent")
        session = make_session(
            responses=[spent] * 5 + ["ok"] * 5, recycle_min_gap=0.0
        )
        async with session:
            results = await asyncio.gather(
                *(session.fetch("GET", "https://example.com/x") for _ in range(5))
            )
        assert results == ["ok"] * 5
        assert session.stats.recycles == 1


class TestAttemptAccounting:
    @pytest.mark.asyncio
    async def test_exhaustion_does_not_consume_an_attempt(self):
        """
        The measured lesson: a spent budget is not a failed request. Charging it
        an attempt means that during a recycle every in-flight request burns its
        budget and is marked failed - the run then finishes reporting success
        with holes in the data, which is the most expensive failure shape there
        is.

        max_attempts=2 here, but six exhaustions pass through before the good
        response. If they were charged, this would raise.
        """
        spent = IdentityExhaustedError("budget spent")
        session = make_session(
            responses=[spent] * 6 + ["ok"], max_attempts=2, recycle_min_gap=0.0
        )
        async with session:
            assert await session.fetch("GET", "https://example.com/x") == "ok"
        assert session.stats.exhaustions == 6

    @pytest.mark.asyncio
    async def test_retryable_rejection_does_consume_attempts(self):
        from curlx import RetryableResponseError

        session = make_session(
            responses=[RetryableResponseError("garbage")] * 5, max_attempts=3
        )
        session.retry.min_wait = 0.0
        session.retry.max_wait = 0.0
        async with session:
            with pytest.raises(RetryExhaustedError):
                await session.fetch("GET", "https://example.com/x")

    @pytest.mark.asyncio
    async def test_terminal_rejection_is_raised_immediately(self):
        session = make_session(responses=[ResponseInvalidError("nope")] * 3, max_attempts=3)
        async with session:
            with pytest.raises(ResponseInvalidError):
                await session.fetch("GET", "https://example.com/x")
            # One attempt only: a terminal verdict must not be retried.
            assert len(session.client.calls) == 1

    @pytest.mark.asyncio
    async def test_recycling_that_does_not_help_falls_back_to_waiting(self):
        """
        Two recycles inside recycle_min_gap mean the fresh identity was spent
        immediately, so the limit is not per-session and opening more sessions
        only adds noise. The session must switch to waiting instead.
        """
        spent = IdentityExhaustedError("budget spent")
        session = make_session(
            responses=[spent, spent, "ok"], recycle_min_gap=3600.0, max_attempts=5
        )
        session.gate.cooldown = 0.0  # do not actually sleep a minute
        async with session:
            assert await session.fetch("GET", "https://example.com/x") == "ok"
        assert session.stats.recycles == 1
        assert session.gate.pauses == 1


class TestStickyKey:
    @pytest.mark.asyncio
    async def test_identity_key_is_used_as_the_sticky_proxy_key(self):
        """
        A bound session that changes egress IP mid-session can lose the very
        binding it warmed up, so the identity and the sticky key must be the
        same thing by default.
        """
        session = make_session(responses=["ok"])
        async with session:
            await session.fetch("GET", "https://example.com/x")
            _, _, kwargs = session.client.calls[0]
        assert kwargs["session_key"] == "north"

    @pytest.mark.asyncio
    async def test_caller_can_override_the_sticky_key(self):
        session = make_session(responses=["ok"])
        async with session:
            await session.fetch("GET", "https://example.com/x", session_key="other")
            _, _, kwargs = session.client.calls[0]
        assert kwargs["session_key"] == "other"

    @pytest.mark.asyncio
    async def test_fetching_after_close_is_a_clear_error(self):
        session = make_session(responses=["ok"])
        async with session:
            pass
        with pytest.raises(RuntimeError, match="not entered"):
            await session.fetch("GET", "https://example.com/x")


class TestQuotaGate:
    @pytest.mark.asyncio
    async def test_concurrent_trips_collapse_into_one_pause(self):
        """
        Every worker probing independently is what keeps a counted quota from
        ever recovering: each probe is itself a request against the budget.
        """
        probes = {"n": 0}

        async def probe():
            probes["n"] += 1
            return True

        gate = QuotaGate(probe=probe, cooldown=0.0)
        await asyncio.gather(*(gate.trip() for _ in range(8)))

        assert gate.pauses == 1
        assert probes["n"] == 1
        assert gate.is_open

    @pytest.mark.asyncio
    async def test_probe_interval_grows(self):
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(d):
            slept.append(d)
            await real_sleep(0)

        attempts = {"n": 0}

        async def probe():
            attempts["n"] += 1
            return attempts["n"] >= 4  # fail three times

        gate = QuotaGate(probe=probe, cooldown=1.0, max_interval=4.0)
        import unittest.mock

        with unittest.mock.patch("asyncio.sleep", fake_sleep):
            await gate.trip()

        # Exponential, then clamped at max_interval.
        assert slept == [1.0, 2.0, 4.0, 4.0]

    @pytest.mark.asyncio
    async def test_wait_blocks_while_closed(self):
        gate = QuotaGate()
        async with gate.closed():
            assert not gate.is_open
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(gate.wait(), timeout=0.05)
        assert gate.is_open

    @pytest.mark.asyncio
    async def test_closed_does_not_reopen_an_already_closed_gate(self):
        """Nested holds must not let the inner one reopen the outer's pause."""
        gate = QuotaGate()
        async with gate.closed():
            async with gate.closed():
                pass
            assert not gate.is_open
        assert gate.is_open

    @pytest.mark.asyncio
    async def test_gate_reopens_after_max_wait_even_without_a_probe_success(self):
        """A permanently shut gate turns a throttle into a hang."""

        async def probe():
            return False

        gate = QuotaGate(probe=probe, cooldown=0.0, max_wait=0.0)
        await gate.trip()
        assert gate.is_open


class TestIdentityPool:
    @staticmethod
    def _pool(**kwargs):
        return IdentityPool(
            [Identity("north"), Identity("south"), Identity("east")],
            client_factory=lambda i: FakeClient(["ok"] * 10),
            rps=10,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_one_shared_limiter(self):
        """
        Giving each of N sessions its own limiter at `rps` multiplies the real
        rate by N. The target measures the total, so the bucket must be one.
        """
        pool = self._pool()
        assert isinstance(pool.limiter, RateLimiter)
        assert pool.limiter.requests_per_second == 10

    @pytest.mark.asyncio
    async def test_gates_are_private_by_default(self):
        """
        A per-session quota running out says nothing about the other sessions.
        One shared gate would stop identities that still have budget.
        """
        pool = self._pool()
        gates = {id(s.gate) for s in pool}
        assert len(gates) == 3

    @pytest.mark.asyncio
    async def test_shared_scope_gives_every_session_the_same_gate(self):
        """Correct when the limit is per-IP: everyone is blocked already."""
        pool = self._pool(gate_scope="shared")
        gates = {id(s.gate) for s in pool}
        assert len(gates) == 1

    def test_duplicate_keys_are_rejected(self):
        with pytest.raises(ValueError, match="unique"):
            IdentityPool(
                [Identity("north"), Identity("north")],
                client_factory=lambda i: FakeClient(),
                rps=5,
            )

    def test_unknown_gate_scope_is_rejected(self):
        with pytest.raises(ValueError, match="gate_scope"):
            self._pool(gate_scope="per-host")

    def test_a_budget_is_required(self):
        with pytest.raises(ValueError, match="rps or limiter"):
            IdentityPool([Identity("north")], client_factory=lambda i: FakeClient())

    @pytest.mark.asyncio
    async def test_entry_and_addressing(self):
        async with self._pool() as pool:
            assert len(pool) == 3
            assert pool["north"].identity.key == "north"
            assert await pool["north"].get("https://example.com/x") == "ok"

    @pytest.mark.asyncio
    async def test_partial_entry_failure_closes_what_was_opened(self):
        """
        Warm-up failing on identity 3 of 3 leaves two live clients holding real
        connections. This unwind is the only place that still knows about them.
        """
        warmed = {"n": 0}

        async def warmup(client, identity):
            warmed["n"] += 1
            if warmed["n"] == 3:
                raise ConnectionFailedError("nope")

        pool = IdentityPool(
            [Identity("a"), Identity("b"), Identity("c")],
            client_factory=lambda i: FakeClient(),
            warmup=warmup,
            rps=5,
        )
        with pytest.raises(ConnectionFailedError):
            await pool.__aenter__()

        opened = [c for c in FakeClient.instances if c.entered]
        assert len(opened) == 3
        # The two that entered successfully are closed; the third was closed by
        # BoundSession.__aenter__ when its own warm-up raised.
        assert all(c.closed for c in opened)

    @pytest.mark.asyncio
    async def test_paused_counts_closed_gates(self):
        async with self._pool() as pool:
            assert pool.paused == 0
            async with pool["north"].gate.closed():
                assert pool.paused == 1
