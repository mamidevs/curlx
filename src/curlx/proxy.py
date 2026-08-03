"""
Proxy rotation strategies.

The rotator is safe to share across threads: every mutation of the pool, the
round-robin cursor and the sticky map happens under a single lock. That matters
because the clients resolve a proxy *per request*, not once per session.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from typing import Any

from curlx.exceptions import ProxyError, ProxyPoolExhaustedError
from curlx.utils import get_logger

logger = get_logger("curlx.proxy")

STRATEGIES = frozenset({"round_robin", "random", "weighted_random"})


@dataclass
class Proxy:
    """Normalized proxy configuration."""

    url: str
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_curl_cffi(self) -> dict[str, str]:
        """Render as the ``{"http": ..., "https": ...}`` dict curl_cffi expects."""
        return {"http": self.url, "https": self.url}


class ProxyRotator:
    """
    Rotate proxies with configurable strategies.

    Supports:
    - round_robin: cycle through proxies sequentially
    - random: pick a random proxy each time
    - weighted_random: pick based on weight

    When every proxy has been invalidated, :class:`ProxyPoolExhaustedError` is
    raised rather than returning ``None``. Falling back to a direct connection
    would leak the real IP of a caller that explicitly asked for a proxy.
    """

    def __init__(
        self,
        proxies: list[str] | list[Proxy],
        *,
        strategy: str = "round_robin",
        sticky_sessions: bool = False,
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
        self._index = 0
        self._sticky_map: dict[str, Proxy] = {}
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._proxies)

    def acquire(self, session_key: str | None = None) -> Proxy:
        """
        Return the :class:`Proxy` to use for the next request.

        Callers keep the object so they can pass its ``url`` to
        :meth:`invalidate` if the request fails at the transport level.
        """
        with self._lock:
            if not self._proxies:
                raise ProxyPoolExhaustedError(
                    "All proxies have been invalidated; refusing to fall back to a "
                    "direct connection. Call restore() or supply new proxies."
                )

            if self.sticky_sessions and session_key:
                proxy = self._sticky_map.get(session_key)
                if proxy is None or proxy not in self._proxies:
                    proxy = self._pick_locked()
                    self._sticky_map[session_key] = proxy
                return proxy

            return self._pick_locked()

    def get(self, session_key: str | None = None) -> dict[str, str]:
        """Return the curl_cffi proxy dict for the next request."""
        return self.acquire(session_key=session_key).to_curl_cffi()

    def _pick_locked(self) -> Proxy:
        """Choose a proxy. Caller must already hold ``self._lock``."""
        if self.strategy == "round_robin":
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index = (self._index + 1) % len(self._proxies)
            return proxy

        if self.strategy == "random":
            return random.choice(self._proxies)

        # weighted_random
        weights = [p.weight for p in self._proxies]
        total = sum(weights)
        if total <= 0:
            return random.choice(self._proxies)
        r = random.uniform(0, total)
        cumulative = 0.0
        for proxy, w in zip(self._proxies, weights, strict=True):
            cumulative += w
            if r <= cumulative:
                return proxy
        return self._proxies[-1]

    def invalidate(self, proxy_url: str) -> None:
        """Mark a proxy as bad (e.g. after a connection error)."""
        with self._lock:
            self._proxies = [p for p in self._proxies if p.url != proxy_url]
            remaining = len(self._proxies)
            self._index = self._index % remaining if remaining else 0
            dead_keys = [k for k, p in self._sticky_map.items() if p.url == proxy_url]
            for k in dead_keys:
                del self._sticky_map[k]
        logger.warning("Proxy invalidated: %s | remaining=%d", proxy_url, remaining)

    def reset(self) -> None:
        """Reset the round-robin cursor and drop all sticky bindings."""
        with self._lock:
            self._index = 0
            self._sticky_map.clear()

    def restore(self) -> None:
        """Bring back every proxy invalidated since construction."""
        with self._lock:
            self._proxies = list(self._original)
            self._index = 0
            self._sticky_map.clear()
        logger.info("Proxy pool restored | count=%d", len(self._original))
