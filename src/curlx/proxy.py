"""
Proxy rotation strategies.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from curlx.exceptions import ProxyError
from curlx.utils import get_logger

logger = get_logger("curlx.proxy")


@dataclass
class Proxy:
    """Normalized proxy configuration."""

    url: str
    weight: float = 1.0
    metadata: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}

    def to_curl_cffi(self) -> Dict[str, str]:
        return {"http": self.url, "https": self.url}


class ProxyRotator:
    """
    Rotate proxies with configurable strategies.

    Supports:
    - round_robin: cycle through proxies sequentially
    - random: pick a random proxy each time
    - weighted_random: pick based on weight
    """

    def __init__(
        self,
        proxies: Union[List[str], List[Proxy]],
        *,
        strategy: str = "round_robin",
        sticky_sessions: bool = False,
    ) -> None:
        if not proxies:
            raise ProxyError("Proxy list cannot be empty")

        self._proxies: List[Proxy] = []
        for p in proxies:
            if isinstance(p, str):
                self._proxies.append(Proxy(url=p))
            else:
                self._proxies.append(p)

        self.strategy = strategy
        self.sticky_sessions = sticky_sessions
        self._index = 0
        self._sticky_map: Dict[str, Proxy] = {}

        if strategy not in {"round_robin", "random", "weighted_random"}:
            raise ProxyError(f"Unknown proxy strategy: {strategy}")

    @property
    def count(self) -> int:
        return len(self._proxies)

    def get(self, session_key: Optional[str] = None) -> Optional[Dict[str, str]]:
        """
        Return proxy dict for curl_cffi.
        If sticky_sessions is enabled and session_key is provided,
        always return the same proxy for that key.
        """
        if not self._proxies:
            return None

        if self.sticky_sessions and session_key:
            proxy = self._sticky_map.get(session_key)
            if proxy is None:
                proxy = self._pick()
                self._sticky_map[session_key] = proxy
            return proxy.to_curl_cffi()

        proxy = self._pick()
        return proxy.to_curl_cffi()

    def _pick(self) -> Proxy:
        if self.strategy == "round_robin":
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            return proxy

        if self.strategy == "random":
            return random.choice(self._proxies)

        # weighted_random
        weights = [p.weight for p in self._proxies]
        total = sum(weights)
        if total == 0:
            return random.choice(self._proxies)
        r = random.uniform(0, total)
        cumulative = 0.0
        for proxy, w in zip(self._proxies, weights):
            cumulative += w
            if r <= cumulative:
                return proxy
        return self._proxies[-1]

    def invalidate(self, proxy_url: str) -> None:
        """Mark a proxy as bad (e.g. after a connection error)."""
        self._proxies = [p for p in self._proxies if p.url != proxy_url]
        if self.sticky_sessions:
            dead_keys = [k for k, p in self._sticky_map.items() if p.url == proxy_url]
            for k in dead_keys:
                del self._sticky_map[k]
        logger.warning("Proxy invalidated: %s | remaining=%d", proxy_url, len(self._proxies))

    def reset(self) -> None:
        """Reset round-robin index."""
        self._index = 0
