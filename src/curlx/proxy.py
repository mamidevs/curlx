"""
Proxy rotation, health and supply.

Three layers, deliberately separable:

``Proxy``/``ProxyRotator`` pick which egress to use and remember which ones are
currently unusable. ``ProxyProvider`` is the protocol for *where proxies come
from*, so a pool can be a literal list or something refreshed from a service
without the client knowing the difference.

The rotator is safe to share across threads: every mutation of the pool, the
round-robin cursor, the health map and the sticky map happens under a single
lock. That matters because the clients resolve a proxy *per request*, not once
per session.

Two failure modes are distinguished, because conflating them retires healthy
proxies. A proxy that cannot carry traffic is broken everywhere. A proxy the
target refuses to serve is broken *at that target only* and still perfectly
good elsewhere - so blame can be scoped to a host.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from curlx.exceptions import ProxyError, ProxyPoolExhaustedError
from curlx.utils import get_logger

logger = get_logger("curlx.proxy")

STRATEGIES = frozenset({"round_robin", "random", "weighted_random"})

#: Ceiling for exponential cooldown, in seconds.
MAX_COOLDOWN = 900.0


@dataclass
class Proxy:
    """Normalized proxy configuration."""

    url: str
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_curl_cffi(self) -> dict[str, str]:
        """Render as the ``{"http": ..., "https": ...}`` dict curl_cffi expects."""
        return {"http": self.url, "https": self.url}


@dataclass
class ProxyHealth:
    """
    What is currently known about one proxy.

    ``cooldown_until`` is a monotonic deadline, not a wall-clock one, so a
    system clock adjustment cannot resurrect a proxy early or strand it.
    """

    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0
    #: Per-host deadlines, for proxies a single target refuses.
    host_cooldown: dict[str, float] = field(default_factory=dict)

    def available(self, now: float, host: str | None = None) -> bool:
        if now < self.cooldown_until:
            return False
        return host is None or now >= self.host_cooldown.get(host, 0.0)


class ProxyRotator:
    """
    Rotate proxies with configurable strategies.

    Supports:
    - round_robin: cycle through proxies sequentially
    - random: pick a random proxy each time
    - weighted_random: pick based on weight

    When no proxy is usable, :class:`ProxyPoolExhaustedError` is raised rather
    than returning ``None``. Falling back to a direct connection would leak the
    real IP of a caller that explicitly asked for a proxy.

    Failure handling depends on ``cooldown``. At the default of ``0`` a failed
    proxy is retired permanently and only ``restore()`` brings it back - simple,
    and right for a small pool of proxies you own. With a positive ``cooldown``
    a failure instead benches the proxy for an exponentially growing interval
    and it returns on its own, which is what a large or rented pool needs:
    permanent retirement there drains the pool over a long run, and the last
    invalidation turns into an outage.
    """

    def __init__(
        self,
        proxies: list[str] | list[Proxy],
        *,
        strategy: str = "round_robin",
        sticky_sessions: bool = False,
        cooldown: float = 0.0,
        max_cooldown: float = MAX_COOLDOWN,
    ) -> None:
        # Validate before building any state, so a bad strategy never leaves a
        # half-constructed rotator behind.
        if strategy not in STRATEGIES:
            raise ProxyError(f"Unknown proxy strategy: {strategy}. Use one of {sorted(STRATEGIES)}")
        if not proxies:
            raise ProxyError("Proxy list cannot be empty")

        self._proxies: list[Proxy] = [Proxy(url=p) if isinstance(p, str) else p for p in proxies]
        self._original: list[Proxy] = list(self._proxies)

        self.strategy = strategy
        self.sticky_sessions = sticky_sessions
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self._index = 0
        self._sticky_map: dict[str, Proxy] = {}
        self._health: dict[str, ProxyHealth] = {}
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._proxies)

    def acquire(self, session_key: str | None = None, host: str | None = None) -> Proxy:
        """
        Return the :class:`Proxy` to use for the next request.

        Args:
            session_key: Sticky binding key. Ignored unless ``sticky_sessions``
                is on. A bound proxy that has since gone unusable is replaced
                and the binding re-pointed, so stickiness never outranks health.
            host: Target host. Proxies benched for this host specifically are
                skipped; proxies benched globally are skipped regardless.

        Raises:
            ProxyPoolExhaustedError: Nothing is usable right now.
        """
        with self._lock:
            usable = self._usable_locked(host)
            if not usable:
                raise ProxyPoolExhaustedError(self._exhausted_message_locked(host))

            if self.sticky_sessions and session_key:
                proxy = self._sticky_map.get(session_key)
                if proxy is None or proxy not in usable:
                    proxy = self._pick_locked(usable)
                    self._sticky_map[session_key] = proxy
                return proxy

            return self._pick_locked(usable)

    def get(self, session_key: str | None = None, host: str | None = None) -> dict[str, str]:
        """Return the curl_cffi proxy dict for the next request."""
        return self.acquire(session_key=session_key, host=host).to_curl_cffi()

    def _usable_locked(self, host: str | None) -> list[Proxy]:
        """Pool members not currently benched. Caller holds ``self._lock``."""
        if self.cooldown <= 0:
            # Failures retire permanently in this mode, so membership already
            # is availability and the health map stays empty.
            return self._proxies
        now = time.monotonic()
        return [
            p
            for p in self._proxies
            if self._health.get(p.url, _HEALTHY).available(now, host)
        ]

    def _exhausted_message_locked(self, host: str | None) -> str:
        if not self._proxies:
            return (
                "All proxies have been invalidated; refusing to fall back to a "
                "direct connection. Call restore() or supply new proxies."
            )
        scope = f" for host {host!r}" if host else ""
        return (
            f"All {len(self._proxies)} proxies are in cooldown{scope}; refusing to "
            f"fall back to a direct connection. Wait for a cooldown to lapse or "
            f"supply new proxies."
        )

    def _pick_locked(self, pool: list[Proxy]) -> Proxy:
        """Choose from ``pool``. Caller must already hold ``self._lock``."""
        if self.strategy == "round_robin":
            proxy = pool[self._index % len(pool)]
            self._index = (self._index + 1) % len(pool)
            return proxy

        if self.strategy == "random":
            return random.choice(pool)

        # weighted_random
        weights = [p.weight for p in pool]
        total = sum(weights)
        if total <= 0:
            return random.choice(pool)
        r = random.uniform(0, total)
        cumulative = 0.0
        for proxy, w in zip(pool, weights, strict=True):
            cumulative += w
            if r <= cumulative:
                return proxy
        return pool[-1]

    def report(
        self,
        proxy_url: str,
        *,
        ok: bool,
        host: str | None = None,
        blocked: bool = False,
    ) -> None:
        """
        Record the outcome of one request, and bench the proxy if warranted.

        Args:
            proxy_url: The proxy that carried (or failed to carry) the request.
            ok: Whether the request succeeded. A success clears the failure
                streak and any cooldown - a proxy that just worked is working.
            host: Target host, needed to scope a block.
            blocked: True when the *target* refused this egress rather than the
                egress failing. Combined with ``host``, benches the proxy only
                for that host; a proxy blocked by one site is still fine for
                every other one, and retiring it globally throws away capacity
                for no reason.
        """
        if self.cooldown <= 0:
            if not ok:
                self.invalidate(proxy_url)
            return

        with self._lock:
            health = self._health.setdefault(proxy_url, ProxyHealth())
            if ok:
                health.successes += 1
                health.failures = 0
                health.cooldown_until = 0.0
                if host is not None:
                    health.host_cooldown.pop(host, None)
                return

            health.failures += 1
            # Exponential in the failure streak, not in wall time: a proxy that
            # fails once and recovers is not punished for an old streak.
            delay = min(self.cooldown * (2 ** (health.failures - 1)), self.max_cooldown)
            deadline = time.monotonic() + delay
            if blocked and host is not None:
                health.host_cooldown[host] = deadline
                scope = f"host={host}"
            else:
                health.cooldown_until = deadline
                scope = "all hosts"
        logger.warning(
            "Proxy benched for %.0fs (%s): %s | failures=%d",
            delay,
            scope,
            proxy_url,
            health.failures,
        )

    def invalidate(self, proxy_url: str) -> None:
        """Mark a proxy as bad (e.g. after a connection error)."""
        with self._lock:
            self._proxies = [p for p in self._proxies if p.url != proxy_url]
            remaining = len(self._proxies)
            self._index = self._index % remaining if remaining else 0
            self._health.pop(proxy_url, None)
            dead_keys = [k for k, p in self._sticky_map.items() if p.url == proxy_url]
            for k in dead_keys:
                del self._sticky_map[k]
        logger.warning("Proxy invalidated: %s | remaining=%d", proxy_url, remaining)

    @property
    def available(self) -> int:
        """How many proxies could serve a request right now, ignoring host scope."""
        with self._lock:
            return len(self._usable_locked(None))

    def reset(self) -> None:
        """Reset the round-robin cursor and drop all sticky bindings."""
        with self._lock:
            self._index = 0
            self._sticky_map.clear()

    def restore(self) -> None:
        """Bring back every proxy invalidated or benched since construction."""
        with self._lock:
            self._proxies = list(self._original)
            self._index = 0
            self._sticky_map.clear()
            self._health.clear()
        logger.info("Proxy pool restored | count=%d", len(self._original))


#: Shared "nothing known, nothing wrong" record, so the common path does not
#: allocate a ProxyHealth per lookup. Never mutated - report() uses setdefault.
_HEALTHY = ProxyHealth()


@runtime_checkable
class ProxyProvider(Protocol):
    """
    Where proxies come from.

    A list you typed into the source and a pool leased from a service differ in
    exactly one respect: the second one changes underneath you. Hiding that
    behind a protocol is what lets the same crawl run against either.

    ``acquire`` and ``report`` are the hot path and are called once per request.
    ``refresh`` is the cold path - a provider backed by a static list makes it a
    no-op, one backed by a service re-reads its pool.
    """

    async def acquire(self, key: str | None = None, host: str | None = None) -> Proxy:
        """Return a proxy for the next request, optionally sticky to ``key``."""
        ...

    async def report(
        self, proxy: Proxy, *, ok: bool, host: str | None = None, blocked: bool = False
    ) -> None:
        """Record how that request went, so the pool can bench bad egresses."""
        ...

    async def refresh(self) -> None:
        """Re-read the pool from its source, if it has one."""
        ...


class StaticProvider:
    """
    A :class:`ProxyProvider` over a fixed list, backed by a :class:`ProxyRotator`.

    Exists so callers can write against the protocol without giving up the
    simple case. ``refresh()`` restores every benched proxy, which is the only
    honest meaning "re-read the source" can have when the source is a literal.
    """

    def __init__(self, rotator: ProxyRotator) -> None:
        self.rotator = rotator

    @classmethod
    def from_urls(cls, urls: list[str], **kwargs: Any) -> StaticProvider:
        """Build a rotator from plain URLs. ``kwargs`` go to :class:`ProxyRotator`."""
        return cls(ProxyRotator(urls, **kwargs))

    async def acquire(self, key: str | None = None, host: str | None = None) -> Proxy:
        return self.rotator.acquire(session_key=key, host=host)

    async def report(
        self, proxy: Proxy, *, ok: bool, host: str | None = None, blocked: bool = False
    ) -> None:
        self.rotator.report(proxy.url, ok=ok, host=host, blocked=blocked)

    async def refresh(self) -> None:
        self.rotator.restore()

    def __repr__(self) -> str:
        return f"StaticProvider({self.rotator.count} proxies)"


class EndpointProvider:
    """
    A :class:`ProxyProvider` that leases its pool from an HTTP endpoint.

    The response shape is not this class's business - every proxy vendor
    invents its own - so the caller supplies ``parse``, which turns a decoded
    body into proxy URLs or :class:`Proxy` objects. That keeps this generic
    instead of coupling curlx to one provider's JSON.

    Refresh is lazy and time-based: the pool is re-read on the first acquire
    after ``ttl`` seconds have passed. A background refresher would need a task
    whose lifetime nobody here owns.

    Args:
        url: Endpoint returning the pool.
        parse: Decoded body -> proxy list. Receives whatever ``json()`` gave,
            or the raw text if the body was not JSON.
        ttl: Seconds a fetched pool stays fresh.
        client_factory: Builds the client used to fetch the pool. Defaults to a
            plain ``AsyncHttpClient``; supply your own to add auth headers.
        **rotator_kwargs: Passed to the internal :class:`ProxyRotator` on every
            refresh - ``cooldown`` in particular is usually wanted here, since
            a leased pool should bench rather than permanently retire.

    Raises:
        ProxyError: The endpoint returned nothing usable and no previous pool
            exists to fall back on.
    """

    def __init__(
        self,
        url: str,
        *,
        parse: Callable[[Any], list[str] | list[Proxy]],
        ttl: float = 300.0,
        client_factory: Callable[[], Any] | None = None,
        **rotator_kwargs: Any,
    ) -> None:
        self.url = url
        self.parse = parse
        self.ttl = ttl
        self.client_factory = client_factory
        self._rotator_kwargs = rotator_kwargs
        self._rotator: ProxyRotator | None = None
        self._fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _ensure_fresh(self) -> ProxyRotator:
        if self._rotator is not None and (time.monotonic() - self._fetched_at) < self.ttl:
            return self._rotator
        async with self._lock:
            # Re-check under the lock: while we waited, another caller may have
            # already refreshed, and a second fetch would only burn a lease.
            if self._rotator is not None and (time.monotonic() - self._fetched_at) < self.ttl:
                return self._rotator
            await self.refresh()
            assert self._rotator is not None  # refresh() raises rather than leaving None
            return self._rotator

    async def refresh(self) -> None:
        """Re-read the pool. Keeps the previous pool if the endpoint fails."""
        # Imported here: curlx.client imports this module, so a module-level
        # import would be circular.
        from curlx.client import AsyncHttpClient

        factory = self.client_factory or (lambda: AsyncHttpClient())
        try:
            async with factory() as client:
                resp = await client.get(self.url)
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
            entries = self.parse(body)
        except Exception as exc:
            if self._rotator is not None:
                logger.warning(
                    "Proxy pool refresh failed, keeping the previous pool: %s", exc
                )
                # Push the clock forward so a hard-down endpoint is not hammered
                # once per request while the old pool is still serving.
                self._fetched_at = time.monotonic()
                return
            raise ProxyError(f"Could not fetch the proxy pool from {self.url}: {exc}") from exc

        if not entries:
            if self._rotator is not None:
                logger.warning("Proxy pool refresh returned nothing, keeping the previous pool")
                self._fetched_at = time.monotonic()
                return
            raise ProxyError(f"Proxy pool at {self.url} is empty")

        self._rotator = ProxyRotator(entries, **self._rotator_kwargs)
        self._fetched_at = time.monotonic()
        logger.info("Proxy pool refreshed | count=%d", self._rotator.count)

    async def acquire(self, key: str | None = None, host: str | None = None) -> Proxy:
        rotator = await self._ensure_fresh()
        return rotator.acquire(session_key=key, host=host)

    async def report(
        self, proxy: Proxy, *, ok: bool, host: str | None = None, blocked: bool = False
    ) -> None:
        if self._rotator is not None:
            self._rotator.report(proxy.url, ok=ok, host=host, blocked=blocked)

    def __repr__(self) -> str:
        count = self._rotator.count if self._rotator is not None else 0
        return f"EndpointProvider({self.url!r}, {count} proxies, ttl={self.ttl}s)"


__all__ = [
    "MAX_COOLDOWN",
    "STRATEGIES",
    "EndpointProvider",
    "Proxy",
    "ProxyHealth",
    "ProxyProvider",
    "ProxyRotator",
    "StaticProvider",
]
