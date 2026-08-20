"""
Bound-identity crawling - one catalogue per region, from one process.

The situation this exists for: a target serves different data depending on a
session cookie you have to go and get, and it counts your requests per session
rather than rating them per second. Three consequences follow, and all three are
what BoundSession/IdentityPool handle.

1. Every region needs its own cookie jar, so it needs its own client. Rebinding
   one shared jar in turn would have concurrent requests overwrite each other's
   context.
2. The rate budget is *not* per region. Giving six sessions 20 rps each is 120
   rps at the target, which is what gets a crawl banned. One shared bucket.
3. "HTTP 200" is not success. A drifted binding, an empty body and a block page
   all arrive as a well-formed 200 and raise nothing - so ``validate`` is where
   the real success criterion lives.

The endpoints below are placeholders. Point them at a real target before running.
"""

import asyncio
from typing import Any

from curlx import (
    AsyncHttpClient,
    BoundSession,
    Identity,
    IdentityExhaustedError,
    IdentityPool,
    RateLimiter,
    ResponseInvalidError,
    RetryableResponseError,
    StaticProvider,
)
from curlx.exceptions import IdentityStaleError

BASE = "https://api.example.com"

# One entry per catalogue. `payload` is opaque to curlx - it is whatever the
# warm-up needs, here the id the target uses for a region.
IDENTITIES = [
    Identity("north", payload=1001),
    Identity("south", payload=1002),
    Identity("east", payload=1003),
]


async def warm_up(client: AsyncHttpClient, identity: Identity) -> None:
    """
    Establish the binding: land on the site, then select the region.

    Called once when the session opens, again whenever the binding drifts, and
    again on every recycle - so it has to be safe to repeat.
    """
    await client.get(f"{BASE}/", headers={"Accept": "text/html,*/*"})
    resp = await client.post(f"{BASE}/session/region", json={"regionId": identity.payload})
    if resp.status_code not in (200, 204):
        # Raising here is deliberate: a session that never bound would return
        # another region's data for hours, and nothing downstream could tell.
        raise RuntimeError(f"{identity.key}: binding failed (HTTP {resp.status_code})")


def make_validator(identity: Identity) -> Any:
    """
    Build the success criterion for one identity.

    This is the part that cannot be generated or shared between sites: only you
    know what a good response from *your* target looks like. Keep it boring and
    explicit - every branch here is a failure mode you would otherwise debug in
    production.
    """

    def validate(resp: Any) -> None:
        if resp.status_code == 429:
            raise IdentityExhaustedError(
                "quota spent", url=resp.url, response=resp, detail="HTTP 429"
            )
        if not resp.content:
            # Empty 200: seen when a required header is missing. Transient
            # enough to be worth another attempt.
            raise RetryableResponseError("empty body", url=resp.url, response=resp)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise RetryableResponseError(
                "body is not JSON", url=resp.url, response=resp, detail=str(exc)
            ) from exc

        if payload.get("blocked"):
            # The egress got filtered, not the account. Blaming the proxy is
            # what lets the pool bench it for this host and move on.
            raise ResponseInvalidError(
                "blocked", url=resp.url, response=resp, blames_proxy=True
            )

        served = payload.get("regionId")
        if served is not None and served != identity.payload:
            # The single most dangerous response there is: a perfectly valid
            # 200 carrying the wrong region's data. Nothing but this check
            # stands between it and the database.
            raise IdentityStaleError(
                "binding drifted",
                url=resp.url,
                response=resp,
                detail=f"regionId={served} != {identity.payload}",
            )

    return validate


def probe_factory(identity: Identity):
    """
    Build the "is the budget back?" check for one identity.

    Sync function returning an async one: the pool calls this once per identity
    at construction, and the gate awaits the result later.

    On its own client, deliberately. Probing through the exhausted session would
    be answered by the very limit being waited on, and every probe would itself
    count against it - which is how a paused crawl never resumes.
    """

    async def probe() -> bool:
        async with AsyncHttpClient(timeout=10) as client:
            resp = await client.get(f"{BASE}/health")
            return resp.status_code == 200

    return probe


async def main() -> None:
    # Shared across every identity: what the target measures is the total rate,
    # not the per-session one.
    limiter = RateLimiter(20, burst=20, per_host=True)

    # Sticky per identity: a bound session that changes egress IP mid-run can
    # lose the very binding it warmed up.
    proxies = StaticProvider.from_urls(
        ["http://user:pass@proxy1.example.com:8080", "http://user:pass@proxy2.example.com:8080"],
        sticky_sessions=True,
        cooldown=120.0,  # bench and recover, rather than retire permanently
    )

    def client_factory(identity: Identity) -> AsyncHttpClient:
        return AsyncHttpClient(
            impersonate="chrome",
            max_concurrent=8,
            rate_limiter=limiter,
            proxies=proxies.rotator,
            validate=make_validator(identity),
            default_headers={"Accept": "application/json"},
            # No CookieJar: identities must not share one file, or their
            # contexts overwrite each other. Warming up is two cheap requests.
        )

    async with IdentityPool(
        IDENTITIES,
        client_factory=client_factory,
        warmup=warm_up,
        limiter=limiter,
        # Per-identity, because the quota here is counted per session cookie.
        # Use "shared" when the limit is per-IP and everyone is blocked at once.
        gate_scope="per-identity",
        probe=probe_factory,
    ) as pool:
        results = await asyncio.gather(
            *(crawl_one(session) for session in pool), return_exceptions=True
        )

    for session, result in zip(pool.sessions, results, strict=True):
        status = f"{len(result)} pages" if not isinstance(result, BaseException) else result
        print(
            f"{session.identity.key:8} {status} | "
            f"requests={session.stats.requests} rewarms={session.stats.rewarms} "
            f"recycles={session.stats.recycles} exhaustions={session.stats.exhaustions}"
        )


async def crawl_one(session: BoundSession) -> list[dict]:
    """Walk one catalogue. Repair happens underneath; this loop never sees it."""
    pages: list[dict] = []
    page = 1
    while True:
        resp = await session.get(f"{BASE}/catalogue", params={"page": page})
        payload = resp.json()
        pages.append(payload)
        if page >= payload.get("pageCount", 1):
            return pages
        page += 1


if __name__ == "__main__":
    asyncio.run(main())
