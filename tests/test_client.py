"""
Tests for AsyncHttpClient and SyncHttpClient.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from curlx import AsyncHttpClient, SyncHttpClient
from curlx.models import Response


class FakeRawResponse:
    """Fake curl_cffi-like response for tests."""

    def __init__(self, status_code: int = 200, content: bytes = b'{"ok": true}'):
        self.status_code = status_code
        self.content = content
        self.url = "https://example.com/test"
        self.headers = {}
        self.cookies = {}
        self.text = content.decode()


class TestAsyncHttpClient:
    @pytest.mark.asyncio
    async def test_context_manager_lifecycle(self):
        async with AsyncHttpClient(max_concurrent=5) as client:
            assert client.max_concurrent == 5
            assert client._session is not None
        assert client._session is None

    @pytest.mark.asyncio
    async def test_get_request(self):
        fake_raw = FakeRawResponse(status_code=200, content=b'{"hello": "world"}')

        async with AsyncHttpClient() as client:
            with patch.object(
                client.session, "request", return_value=fake_raw
            ) as mock_req:
                resp = await client.get("https://example.com/test")
                assert resp.status_code == 200
                assert resp.json() == {"hello": "world"}
                mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_all(self):
        urls = [f"https://example.com/{i}" for i in range(3)]
        fake_raw = FakeRawResponse()

        async with AsyncHttpClient(max_concurrent=10) as client:
            with patch.object(client.session, "request", return_value=fake_raw):
                results = await client.fetch_all(urls)
                assert len(results) == 3
                for r in results:
                    assert isinstance(r, Response)


class TestSyncHttpClient:
    def test_context_manager_lifecycle(self):
        with SyncHttpClient() as client:
            assert client._session is not None

    def test_get_request(self):
        fake_raw = FakeRawResponse(status_code=200, content=b'{"sync": true}')

        with SyncHttpClient() as client:
            with patch.object(client.session, "request", return_value=fake_raw) as mock_req:
                resp = client.get("https://example.com/test")
                assert resp.status_code == 200
                assert resp.json() == {"sync": True}
                mock_req.assert_called_once()
