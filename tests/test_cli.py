"""
Tests for curlx CLI.
"""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from curlx.cli import app

runner = CliRunner()


class FakeResponse:
    """Fake Response for CLI tests."""

    def __init__(self, status_code: int = 200, content: bytes = b'{"ok": true}'):
        self.status_code = status_code
        self.content = content
        self.url = "https://httpbin.org/get"
        self.headers = {"content-type": "application/json"}
        self.cookies = {}
        self.text = content.decode()


def test_get_success():
    fake_raw = FakeResponse(200, b'{"hello": "world"}')
    with patch("curlx.cli.SyncHttpClient") as MockClient:
        instance = MagicMock()
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=None)
        instance.request = MagicMock(return_value=fake_raw)
        MockClient.return_value = instance

        result = runner.invoke(app, ["get", "https://httpbin.org/get"])
        assert result.exit_code == 0
        assert "200" in result.output or "hello" in result.output


def test_post_with_json():
    fake_raw = FakeResponse(201, b'{"created": true}')
    with patch("curlx.cli.SyncHttpClient") as MockClient:
        instance = MagicMock()
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=None)
        instance.request = MagicMock(return_value=fake_raw)
        MockClient.return_value = instance

        result = runner.invoke(app, [
            "post", "https://httpbin.org/post",
            "--json", '{"name":"test"}',
        ])
        assert result.exit_code == 0
        assert instance.request.called
        _, kwargs = instance.request.call_args
        assert kwargs["json"] == {"name": "test"}


def test_profiles_command():
    result = runner.invoke(app, ["profiles"])
    assert result.exit_code == 0
    assert "chrome" in result.output


def test_user_agents_command():
    result = runner.invoke(app, ["user-agents", "chrome"])
    assert result.exit_code == 0
    assert "Mozilla" in result.output


def test_invalid_json_body():
    result = runner.invoke(app, [
        "post", "https://httpbin.org/post",
        "--json", "not valid json",
    ])
    assert result.exit_code == 1
    assert "Invalid JSON" in result.output


def test_verbose_flag():
    fake_raw = FakeResponse(200, b'{"verbose": true}')
    with patch("curlx.cli.SyncHttpClient") as MockClient:
        instance = MagicMock()
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=None)
        instance.request = MagicMock(return_value=fake_raw)
        MockClient.return_value = instance

        result = runner.invoke(app, ["get", "https://httpbin.org/get", "-v"])
        assert result.exit_code == 0
