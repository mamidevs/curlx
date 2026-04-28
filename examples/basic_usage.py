"""
Basic usage examples for curlx.
"""

import asyncio

from curlx import AsyncHttpClient, SyncHttpClient


def sync_example():
    with SyncHttpClient(impersonate="chrome137", timeout=10) as client:
        resp = client.get("https://httpbin.org/get")
        print("Status:", resp.status_code)
        print("JSON:", resp.json())


async def async_example():
    async with AsyncHttpClient(
        impersonate="chrome137",
        max_concurrent=20,
        timeout=10,
    ) as client:
        resp = await client.get("https://httpbin.org/get")
        print("Status:", resp.status_code)
        print("JSON:", resp.json())


async def concurrent_fetch():
    urls = [
        "https://httpbin.org/get",
        "https://httpbin.org/ip",
        "https://httpbin.org/user-agent",
    ]
    async with AsyncHttpClient(max_concurrent=5) as client:
        results = await client.fetch_all(urls)
        for url, resp in zip(urls, results):
            print(f"{url} -> {resp}")


if __name__ == "__main__":
    print("=== Sync ===")
    sync_example()

    print("\n=== Async ===")
    asyncio.run(async_example())

    print("\n=== Concurrent ===")
    asyncio.run(concurrent_fetch())
