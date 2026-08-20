"""
Bound sessions: a client that carries an identity it can repair and replace.

A plain client is stateless as far as the target is concerned. Many targets are
not: they hand out a session cookie that binds the connection to a store, a
region, a tenant or an account, and every later response is scoped by it. That
binding has three properties no HTTP client models on its own.

It must be *established* before the session is usable - a warm-up, whatever
shape it takes. It can *drift*, and the drift is silent: the backend keeps
answering 200, just with somebody else's data. And it can be *spent*, when the
quota is counted per identity rather than rated per second, in which case
slowing down buys nothing at all and only a fresh identity resets the counter.

:class:`BoundSession` owns those three. :class:`IdentityPool` runs N of them
against one shared rate budget, which is the shape every multi-tenant crawl
converges on.

The site-specific parts stay outside: what the warm-up does, what counts as
drift, and how to tell that a quota came back are all caller-supplied
callables. This module owns only the control flow around them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from curlx.client import AsyncHttpClient
from curlx.exceptions import IdentityExhaustedError, IdentityStaleError
from curlx.models import Response
from curlx.ratelimit import RateLimiter
from curlx.retry import RetryPolicy
from curlx.utils import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger("curlx.identity")

#: Hook that establishes (or re-establishes) the binding on a client.
WarmupHook = Callable[[AsyncHttpClient, "Identity"], Awaitable[None]]

#: Cheap check answering "is the budget back yet?". True reopens the gate.
ProbeHook = Callable[[], Awaitable[bool]]

_DEFAULT_MAX_ATTEMPTS = 4
_DEFAULT_MAX_RECYCLES = 60
_DEFAULT_RECYCLE_MIN_GAP = 20.0


@dataclass(frozen=True)
class Identity:
    """
    One addressable identity a session can be bound to.

    Args:
        key: Stable label. Doubles as the sticky-proxy key, so two identities
            must never share one - they would share an egress IP and, on a
            target that binds by IP, each other's binding.
        payload: Whatever the caller needs to warm this identity up - a store
            id, a region, a set of credentials. Opaque here on purpose: the
            moment this module understands the payload it stops being generic.
    """

    key: str
    payload: Any = None

    def __str__(self) -> str:
        return self.key


class QuotaGate:
    """
    A single-flight pause shared by everyone who would otherwise hammer a
    closed door.

    When a budget runs out, the naive reaction is for each worker to back off
    independently. That fails in a specific way: every one of them keeps
    issuing requests to find out whether the door reopened, every one of those
    requests is itself counted, and the budget never recovers. So the pause is
    collective - the first caller to notice closes the gate, one prober decides
    when to reopen it, and everybody else simply waits.

    The probe interval grows exponentially for the same reason. A tight probe
    loop can feed the very limit it is waiting on.

    Scope is deliberately not modelled here. Whether one gate covers a single
    identity or every identity at once depends on what the target counts, which
    only the caller knows; :class:`IdentityPool` exposes that as ``gate_scope``.
    """

    def __init__(
        self,
        *,
        probe: ProbeHook | None = None,
        cooldown: float = 60.0,
        max_wait: float = 21600.0,
        max_interval: float = 600.0,
    ) -> None:
        """
        Args:
            probe: Returns True when the budget is available again. ``None``
                means "wait ``cooldown`` seconds once, then reopen blindly".
            cooldown: First probe interval, in seconds.
            max_wait: Hard ceiling on one pause. Reached, the gate reopens
                anyway - staying shut forever converts a throttle into a hang.
            max_interval: Ceiling for the exponential probe interval.
        """
        self.probe = probe
        self.cooldown = cooldown
        self.max_wait = max_wait
        self.max_interval = max_interval
        self.pauses = 0
        self.paused_seconds = 0.0
        self._open = asyncio.Event()
        self._open.set()
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        """Whether requests may proceed right now."""
        return self._open.is_set()

    async def wait(self) -> None:
        """Block until the gate is open. A no-op when it already is."""
        await self._open.wait()

    async def trip(self, on_pause: Callable[[float], None] | None = None) -> None:
        """
        Close the gate, wait for the budget, reopen.

        Concurrent callers collapse into one pause: whoever takes the lock
        second finds the gate already reopened and returns immediately, rather
        than starting a second pause behind the first.

        Args:
            on_pause: Called with elapsed seconds before each probe, for
                progress reporting. Exceptions from it are not caught - a
                reporting bug should be visible, not swallowed into a pause.
        """
        # Two ways another caller can already be handling this, and both need
        # closing or N workers hitting one limit produce N pauses.
        #
        # A pause is running right now: join it rather than queueing another
        # behind it. Waiting on the event is exactly what the other workers do.
        if not self._open.is_set():
            await self._open.wait()
            return

        # A pause started and finished while we waited for the lock. Comparing
        # the counter catches that; comparing "is it closed?" would not, since
        # by then the gate is open again.
        seen = self.pauses
        async with self._lock:
            if self.pauses != seen or not self._open.is_set():
                return
            self._open.clear()
            self.pauses += 1
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                interval = self.cooldown
                waited = 0.0
                while waited < self.max_wait:
                    if on_pause is not None:
                        on_pause(waited)
                    await asyncio.sleep(interval)
                    waited = loop.time() - started
                    if self.probe is None or await self.probe():
                        return
                    interval = min(interval * 2, self.max_interval)
                logger.warning(
                    "Quota gate reopening after max_wait=%.0fs without a successful probe",
                    self.max_wait,
                )
            finally:
                self.paused_seconds += loop.time() - started
                self._open.set()

    @asynccontextmanager
    async def closed(self) -> AsyncIterator[None]:
        """
        Hold the gate shut for the duration of the block.

        Used while an identity is being replaced: its workers must not issue
        requests against a session that is mid-swap.
        """
        was_open = self._open.is_set()
        self._open.clear()
        try:
            yield
        finally:
            if was_open:
                self._open.set()

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return f"QuotaGate({state}, pauses={self.pauses}, paused={self.paused_seconds:.1f}s)"


@dataclass
class SessionStats:
    """Counters a progress display can read without touching session internals."""

    requests: int = 0
    rewarms: int = 0
    recycles: int = 0
    exhaustions: int = 0
    stale_hits: int = 0


class BoundSession:
    """
    One identity, one client, and the loop that keeps them usable.

    The retry loop lives here rather than inside the client for one reason:
    two of the four failure verdicts cannot be resolved by re-issuing the
    request. A stale identity needs a warm-up first; a spent identity needs a
    replacement. Both require state the client does not have, so the actor that
    holds the state runs the loop.

    Failure handling, by exception type:

    ==============================  =========================================
    ``IdentityStaleError``          re-warm once (single-flight), then retry
    ``IdentityExhaustedError``      recycle or wait, then retry *for free*
    ``RetryableResponseError``      retry with backoff
    ``ResponseInvalidError``        terminal - raised to the caller
    transport errors                retry with backoff, per ``RetryPolicy``
    ==============================  =========================================

    Note the "for free": exhaustion does not consume an attempt. It is not a
    failure of the request, it is the budget ending; charging it means that
    during a recycle every in-flight request burns its attempts and is marked
    failed, and the run finishes reporting success with holes in the data.

    Example:
        >>> async def warm(client, identity):  # doctest: +SKIP
        ...     await client.post("https://api.example.com/select",
        ...                       json={"region": identity.payload})
        >>> session = BoundSession(                        # doctest: +SKIP
        ...     Identity("north", payload=42),
        ...     client_factory=lambda i: AsyncHttpClient(impersonate="chrome"),
        ...     warmup=warm,
        ... )
    """

    def __init__(
        self,
        identity: Identity,
        *,
        client_factory: Callable[[Identity], AsyncHttpClient],
        warmup: WarmupHook | None = None,
        gate: QuotaGate | None = None,
        retry: RetryPolicy | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        max_recycles: int = _DEFAULT_MAX_RECYCLES,
        recycle_min_gap: float = _DEFAULT_RECYCLE_MIN_GAP,
        recycle_on_exhaustion: bool = True,
    ) -> None:
        """
        Args:
            identity: What this session is bound to.
            client_factory: Builds a fresh, un-entered client for an identity.
                Called once at entry and again on every recycle, so it must be
                safe to call repeatedly.
            warmup: Establishes the binding. ``None`` means the identity needs
                no warm-up, and ``rewarm``/``recycle`` then only replace the
                client's cookie jar.
            gate: Shared or private pause. One is created if omitted.
            retry: Backoff policy. A default is built if omitted; supply your
                own to change ``min_wait``/``max_wait`` or to be notified.
            max_attempts: Attempts per request, excluding free exhaustion
                rounds.
            max_recycles: Ceiling on identity replacements for the whole
                session's life. Reaching it is the signal that recycling is not
                helping and the limit lives somewhere above the session.
            recycle_min_gap: Minimum seconds between recycles. Two in quick
                succession mean the fresh identity was exhausted immediately,
                which recycling again cannot fix.
            recycle_on_exhaustion: When False, exhaustion waits on the gate
                instead of replacing the identity. Correct when the budget is
                per-IP rather than per-session, where a new session changes
                nothing.
        """
        self.identity = identity
        self.client_factory = client_factory
        self.warmup = warmup
        self.gate = gate or QuotaGate()
        self.max_attempts = max_attempts
        self.max_recycles = max_recycles
        self.recycle_min_gap = recycle_min_gap
        self.recycle_on_exhaustion = recycle_on_exhaustion
        self.stats = SessionStats()

        self.retry = retry or RetryPolicy(
            func_name=f"BoundSession[{identity.key}]",
            max_attempts=max_attempts,
            min_wait=0.5,
            max_wait=8.0,
        )

        self.client: AsyncHttpClient | None = None
        # Generation counters. A repair is only performed if nobody else has
        # already performed one since the caller last looked - see _repair_once.
        self._warm_generation = 0
        self._recycle_generation = 0
        self._warm_lock = asyncio.Lock()
        self._recycle_lock = asyncio.Lock()
        self._last_recycle = float("-inf")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> BoundSession:
        client = self.client_factory(self.identity)
        await client.__aenter__()
        try:
            await self._run_warmup(client)
        except BaseException:
            await client.aclose()
            raise
        self.client = client
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client. Idempotent."""
        client, self.client = self.client, None
        if client is not None:
            await client.aclose()

    async def _run_warmup(self, client: AsyncHttpClient) -> None:
        if self.warmup is not None:
            await self.warmup(client, self.identity)

    # ------------------------------------------------------------------
    # Repair
    # ------------------------------------------------------------------
    async def rewarm(self) -> None:
        """Re-run the warm-up on the current client."""
        client = self.client
        if client is None:
            return
        await self._run_warmup(client)
        self.stats.rewarms += 1

    async def recycle(self) -> None:
        """
        Replace the identity's client with a freshly warmed one.

        Order matters and is the whole point of this method. The new client is
        created **and warmed** before it becomes ``self.client``; only then is
        the old one closed. Swapping first and warming second leaves a window -
        a few hundred milliseconds is enough - in which workers issue requests
        against an unwarmed session. Those come back wrong, which looks like
        drift, which triggers another recycle, which opens another window.

        Throughput is unaffected: the rate limiter is shared and knows nothing
        about identities, so the same budget keeps flowing. The cost is two
        requests per replacement.
        """
        new = self.client_factory(self.identity)
        await new.__aenter__()
        try:
            await self._run_warmup(new)
        except BaseException:
            await new.aclose()
            raise

        old, self.client = self.client, new
        self.stats.recycles += 1
        self._last_recycle = asyncio.get_running_loop().time()
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                # The replacement already works; a failure to close the corpse
                # must not take down a session that is otherwise healthy.
                logger.warning("Closing the recycled client failed", exc_info=True)

    async def _rewarm_once(self, seen: int) -> None:
        """
        Re-warm unless somebody already did since ``seen``.

        The generation check collapses *concurrent* repairs only. A failure
        arriving after a repair completed legitimately repairs again - that is
        a new problem, not a duplicate of the old one. Sequential failures
        therefore produce sequential repairs, bounded by ``recycle_min_gap``
        and ``max_recycles`` on the recycle path.
        """
        async with self._warm_lock:
            if self._warm_generation != seen:
                return
            await self.rewarm()
            self._warm_generation += 1

    async def _recycle_once(self, seen: int) -> None:
        """
        Replace the identity unless somebody already did since ``seen``.

        Falls back to waiting when replacement is not working: two recycles
        inside ``recycle_min_gap`` mean the fresh identity was exhausted almost
        immediately, so the limit is not per-session after all and opening more
        sessions only adds noise.
        """
        async with self._recycle_lock:
            if self._recycle_generation != seen:
                return
            self._recycle_generation += 1

            if not self.recycle_on_exhaustion:
                await self.gate.trip()
                return

            now = asyncio.get_running_loop().time()
            too_soon = (now - self._last_recycle) < self.recycle_min_gap
            if too_soon or self.stats.recycles >= self.max_recycles:
                logger.info(
                    "Recycling is not clearing the limit for %s (recycles=%d); waiting instead",
                    self.identity.key,
                    self.stats.recycles,
                )
                await self.gate.trip()
                return

            async with self.gate.closed():
                await self.recycle()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    async def fetch(self, method: str, url: str, **kwargs: Any) -> Response:
        """
        Issue one request, repairing the identity as needed.

        The sticky-proxy key is set from the identity unless the caller
        overrides it, so a bound session keeps one egress IP - changing IP
        mid-session is itself a way to lose the binding.

        Raises:
            ResponseInvalidError: The caller's validator rejected the response
                terminally.
            RetryExhaustedError: Attempts ran out on a retryable failure.
            RuntimeError: The session is not entered.
        """
        kwargs.setdefault("session_key", self.identity.key)
        attempt = 0
        # Exhaustion rounds are free (see below), which without a separate
        # bound is an unbounded loop against a target that answers "spent"
        # forever. Recycling and the gate both bound it in wall-clock terms;
        # this bounds it in rounds, so a pathological target cannot spin.
        free_rounds = 0
        max_free_rounds = self.max_recycles + self.max_attempts
        last: Exception | None = None

        while attempt < self.max_attempts:
            attempt += 1
            warm_seen = self._warm_generation
            recycle_seen = self._recycle_generation

            await self.gate.wait()
            # Read once. Re-reading self.client mid-request would race the swap
            # in recycle(); capturing it also makes a concurrent close visible
            # as a clean exit rather than an AttributeError storm.
            client = self.client
            if client is None:
                raise RuntimeError(
                    f"BoundSession[{self.identity.key}] is not entered "
                    f"(use 'async with', and do not fetch after aclose)"
                )

            try:
                resp = await client.request(method, url, **kwargs)
            except IdentityExhaustedError as exc:
                # Free round: the budget ended, the request did not fail.
                # Charging an attempt here means every request in flight during
                # a recycle burns its budget and is marked failed - the run then
                # reports success with holes in the data.
                last = exc
                self.stats.exhaustions += 1
                free_rounds += 1
                if free_rounds > max_free_rounds:
                    raise
                attempt -= 1
                await self._recycle_once(recycle_seen)
                continue
            except IdentityStaleError as exc:
                last = exc
                self.stats.stale_hits += 1
                await self._rewarm_once(warm_seen)
                continue
            except Exception as exc:
                last = exc
                decision = self.retry.decide(exc, attempt)
                if decision.propagate:
                    raise
                if decision.wait is None:
                    break
                await asyncio.sleep(decision.wait)
                continue

            self.stats.requests += 1
            return resp

        if last is None:  # unreachable: max_attempts >= 1 guarantees one round
            raise RuntimeError(f"BoundSession[{self.identity.key}] made no attempt")
        # Raises RetryExhaustedError unless the caller's policy defines a
        # fallback, in which case that value is the response.
        return cast(Response, self.retry.give_up(last, attempt))

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.fetch("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Response:
        return await self.fetch("POST", url, **kwargs)

    def __repr__(self) -> str:
        return (
            f"BoundSession({self.identity.key!r}, requests={self.stats.requests}, "
            f"rewarms={self.stats.rewarms}, recycles={self.stats.recycles})"
        )


class IdentityPool:
    """
    N bound sessions sharing one rate budget.

    Two topology decisions, both learned the expensive way:

    **The limiter is shared, and the pool enforces it.** Giving each of N
    sessions its own limiter at ``rps`` multiplies the real request rate by N.
    What a target measures is the total, so the budget has to be a single token
    bucket that every session draws from - and the pool installs it on every
    client rather than trusting the factory to remember.

    **The gate is not, by default.** When the quota is counted per session
    cookie, one identity running out says nothing about the others; a single
    shared gate would stop five sessions that still have budget. When the limit
    is per-IP instead, the opposite holds and every session is already blocked -
    pass ``gate_scope="shared"`` for that case.

    Example:
        >>> async with IdentityPool(                       # doctest: +SKIP
        ...     [Identity("north"), Identity("south")],
        ...     client_factory=make_client,
        ...     warmup=warm,
        ...     rps=20,
        ... ) as pool:
        ...     resp = await pool["north"].get("https://api.example.com/items")
    """

    def __init__(
        self,
        identities: list[Identity],
        *,
        client_factory: Callable[[Identity], AsyncHttpClient],
        warmup: WarmupHook | None = None,
        rps: float | None = None,
        burst: int | None = None,
        limiter: RateLimiter | None = None,
        gate_scope: str = "per-identity",
        probe: Callable[[Identity], ProbeHook] | None = None,
        **session_kwargs: Any,
    ) -> None:
        """
        Args:
            identities: One session is built per entry. Keys must be unique -
                they address the session and key the sticky proxy.
            client_factory: Builds an un-entered client for an identity. The
                pool installs the shared limiter on whatever it returns, on
                every call including recycles, so the factory does not have to
                thread it through - but a factory that installs a *different*
                limiter is rejected rather than overridden.
            warmup: Applied to every session.
            rps: Shared sustained rate. Ignored if ``limiter`` is given.
            burst: Bucket capacity. Defaults to ``ceil(rps)``.
            limiter: Pre-built shared limiter, if you need one shared with code
                outside this pool.
            gate_scope: ``"per-identity"`` or ``"shared"``.
            probe: Builds the "is the budget back?" check for one identity.
            **session_kwargs: Forwarded verbatim to every
                :class:`BoundSession`.

        Raises:
            ValueError: Duplicate identity keys, an unknown ``gate_scope``, or
                neither ``rps`` nor ``limiter``.
        """
        keys = [i.key for i in identities]
        duplicates = {k for k in keys if keys.count(k) > 1}
        if duplicates:
            raise ValueError(
                f"Identity keys must be unique; duplicated: {sorted(duplicates)}. "
                f"Two sessions sharing a key also share a sticky proxy."
            )
        if gate_scope not in ("per-identity", "shared"):
            raise ValueError(
                f"gate_scope must be 'per-identity' or 'shared', got {gate_scope!r}"
            )
        if limiter is None and rps is None:
            raise ValueError("Pass either rps or limiter")

        self.limiter = limiter or RateLimiter(
            float(rps or 0), burst=burst, per_host=True
        )
        self.gate_scope = gate_scope
        shared_gate = QuotaGate() if gate_scope == "shared" else None
        bound_factory = self._bind_limiter(client_factory)

        self._sessions: dict[str, BoundSession] = {}
        for identity in identities:
            gate = shared_gate or QuotaGate(
                probe=probe(identity) if probe is not None else None
            )
            self._sessions[identity.key] = BoundSession(
                identity,
                client_factory=bound_factory,
                warmup=warmup,
                gate=gate,
                **session_kwargs,
            )
        self._entered: list[BoundSession] = []

    def _bind_limiter(
        self, factory: Callable[[Identity], AsyncHttpClient]
    ) -> Callable[[Identity], AsyncHttpClient]:
        """
        Force every client the factory produces onto the shared budget.

        Without this the pool's central promise is unenforced: the limiter would
        be an attribute nobody reads, and a caller passing ``rps=20`` with a
        plain factory would get N clients with *no* limiter - unlimited rate,
        which is worse than the N-times overshoot the shared budget exists to
        prevent. It has to wrap the factory rather than patch the first client,
        because recycling builds new ones for the session's whole life.

        A factory that installed a *different* limiter is an error rather than
        something to silently override: it means the caller believes in a budget
        this pool is about to ignore.
        """

        def wrapped(identity: Identity) -> AsyncHttpClient:
            client = factory(identity)
            existing = getattr(client, "rate_limiter", None)
            if existing is not None and existing is not self.limiter:
                raise ValueError(
                    f"client_factory gave identity {identity.key!r} its own rate "
                    f"limiter, but IdentityPool shares one across every identity. "
                    f"Pass the pool's limiter through the factory, or hand the "
                    f"pool your limiter with limiter=."
                )
            client.rate_limiter = self.limiter
            return client

        return wrapped

    async def __aenter__(self) -> IdentityPool:
        for session in self._sessions.values():
            try:
                await session.__aenter__()
            except BaseException:
                # Warming up identity 4 of 6 failed. The three already open
                # hold real connections; unwinding them here is the only place
                # that still knows they exist.
                await self._close_entered()
                raise
            self._entered.append(session)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._close_entered()

    async def _close_entered(self) -> None:
        results = await asyncio.gather(
            *(s.aclose() for s in self._entered), return_exceptions=True
        )
        self._entered.clear()
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Closing a bound session failed: %s", result)

    def __getitem__(self, key: str) -> BoundSession:
        return self._sessions[key]

    def __iter__(self) -> Iterator[BoundSession]:
        return iter(self._sessions.values())

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def sessions(self) -> list[BoundSession]:
        return list(self._sessions.values())

    @property
    def paused(self) -> int:
        """How many sessions are currently waiting on a closed gate."""
        return sum(1 for s in self._sessions.values() if not s.gate.is_open)

    def __repr__(self) -> str:
        return (
            f"IdentityPool({len(self)} identities, gate_scope={self.gate_scope!r}, "
            f"paused={self.paused})"
        )


__all__ = [
    "BoundSession",
    "Identity",
    "IdentityPool",
    "ProbeHook",
    "QuotaGate",
    "SessionStats",
    "WarmupHook",
]
