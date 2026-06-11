"""Unit tests for tools.mcp_client.MCPClient.

Strategy: mock the underlying mcp SDK's ClientSession by patching
tools.mcp_client._open_session so MCPClient sees an in-memory fake.
"""
import json
import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from tools.mcp_client import MCPClient
from tools.mcp_exceptions import (
    MCPTransportError, MCPProtocolError, MCPToolError,
)


class _FakeContent:
    def __init__(self, payload):
        self.type = "text"
        self.text = json.dumps(payload)


class _FakeToolResult:
    def __init__(self, payload, is_error=False):
        self.content = [_FakeContent(payload)]
        self.isError = is_error


class _FakeSession:
    def __init__(self, tool_response):
        self._tool_response = tool_response
        self.calls = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if isinstance(self._tool_response, Exception):
            raise self._tool_response
        return self._tool_response

    async def list_tools(self):
        m = MagicMock()
        m.tools = []
        return m


def _install_fake_session(monkeypatch, session):
    async def _open(_endpoint, _timeout):
        async def _closer():
            return None
        return session, _closer

    monkeypatch.setattr("tools.mcp_client._open_session", _open)


def test_call_tool_happy_path(monkeypatch):
    payload = {"hello": "world", "n": 3}
    session = _FakeSession(_FakeToolResult(payload))
    _install_fake_session(monkeypatch, session)

    client = MCPClient(endpoint="http://fake/mcp", timeout=5, retries=0)
    result = client.call_tool("umodel_get_logs", {"entity_id": "x"})

    assert result == payload
    assert session.calls == [("umodel_get_logs", {"entity_id": "x"})]


def test_transport_retry_then_failure(monkeypatch):
    """Connection errors retry N times then raise MCPTransportError."""
    attempts = {"n": 0}

    async def _flaky_open(_endpoint, _timeout):
        attempts["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setattr("tools.mcp_client._open_session", _flaky_open)
    async def _noop_sleep(_s): return None
    monkeypatch.setattr("tools.mcp_client.asyncio.sleep", _noop_sleep)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=3)
    with pytest.raises(MCPTransportError):
        client.call_tool("any_tool", {})
    assert attempts["n"] == 4   # 1 original + 3 retries


def test_transport_retry_then_success(monkeypatch):
    """First two attempts fail with transport error, third succeeds."""
    state = {"calls": 0}

    real_session = _FakeSession(_FakeToolResult({"ok": 1}))

    async def _open(_endpoint, _timeout):
        state["calls"] += 1
        if state["calls"] < 3:
            raise ConnectionError("transient")
        async def _closer(): return None
        return real_session, _closer

    monkeypatch.setattr("tools.mcp_client._open_session", _open)
    async def _noop_sleep(_s): return None
    monkeypatch.setattr("tools.mcp_client.asyncio.sleep", _noop_sleep)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=3)
    result = client.call_tool("umodel_get_logs", {})
    assert result == {"ok": 1}
    assert state["calls"] == 3


def test_tool_semantic_error_does_not_retry(monkeypatch):
    """isError=True is surfaced as MCPToolError on first attempt; no retry."""
    state = {"calls": 0}

    async def _open(_endpoint, _timeout):
        state["calls"] += 1
        async def _closer(): return None
        return _FakeSession(_FakeToolResult({"err": "boom"}, is_error=True)), _closer

    monkeypatch.setattr("tools.mcp_client._open_session", _open)
    async def _noop_sleep(_s): return None
    monkeypatch.setattr("tools.mcp_client.asyncio.sleep", _noop_sleep)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=3)
    with pytest.raises(MCPToolError):
        client.call_tool("any_tool", {})
    assert state["calls"] == 1   # NO retries


def test_protocol_error_wraps_unexpected_exception(monkeypatch):
    """Non-transport, non-tool exceptions become MCPProtocolError."""

    class _BadSession(_FakeSession):
        async def call_tool(self, name, arguments):
            raise ValueError("schema mismatch")

    async def _open(_endpoint, _timeout):
        async def _closer(): return None
        return _BadSession(None), _closer

    monkeypatch.setattr("tools.mcp_client._open_session", _open)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=0)
    with pytest.raises(MCPProtocolError):
        client.call_tool("any_tool", {})


def test_call_tool_safe_inside_async_context(monkeypatch):
    """Regression: call_tool must work even when caller is inside async context."""
    import asyncio
    payload = {"ok": True}
    session = _FakeSession(_FakeToolResult(payload))
    _install_fake_session(monkeypatch, session)

    async def caller():
        client = MCPClient(endpoint="http://fake/mcp", timeout=5, retries=0)
        # This used to crash with "asyncio.run() cannot be called from a running event loop"
        return client.call_tool("any", {})

    result = asyncio.run(caller())
    assert result == payload
