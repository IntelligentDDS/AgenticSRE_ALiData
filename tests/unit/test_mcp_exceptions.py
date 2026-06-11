import pytest
from tools.mcp_exceptions import (
    MCPError, MCPTransportError, MCPProtocolError,
    MCPToolError, EntityNotFound, AuthError, QueryError,
)


def test_hierarchy_roots():
    assert issubclass(MCPTransportError, MCPError)
    assert issubclass(MCPProtocolError, MCPError)
    assert issubclass(MCPToolError, MCPError)


def test_tool_error_subclasses():
    assert issubclass(EntityNotFound, MCPToolError)
    assert issubclass(AuthError, MCPToolError)
    assert issubclass(QueryError, MCPToolError)


def test_tool_error_carries_payload():
    err = MCPToolError(
        message="boom", tool_name="umodel_get_logs",
        code="EntityNotFound", raw={"foo": "bar"},
    )
    assert err.tool_name == "umodel_get_logs"
    assert err.code == "EntityNotFound"
    assert err.raw == {"foo": "bar"}
    assert "boom" in str(err)


def test_tool_error_inherits_message_from_args():
    err = AuthError(
        message="bad ak", tool_name="introduction",
        code="Unauthorized", raw={"http_status": 401},
    )
    assert isinstance(err, MCPToolError)
    assert isinstance(err, MCPError)
    assert err.code == "Unauthorized"
