"""Exception hierarchy for the MCP backend layer.

Transport / Protocol / ToolSemantic errors are surfaced to agents as-is.
The adapter (`tools/mcp_observability.py`) is responsible for mapping
MCP tool error codes to the business exceptions defined here.
"""
from __future__ import annotations
from typing import Any, Dict, Optional


class MCPError(Exception):
    """Base for all MCP-layer errors."""


class MCPTransportError(MCPError):
    """Connection / read timeout / 5xx response after all retries exhausted."""


class MCPProtocolError(MCPError):
    """JSON-RPC malformed, schema mismatch, or session init failure."""


class MCPToolError(MCPError):
    """A tool invocation returned a semantic error.

    Attributes:
        tool_name: MCP tool that failed (e.g. ``umodel_get_logs``)
        code: server-supplied error code string
        raw: original error payload for debugging
    """

    def __init__(
        self,
        message: str = "",
        *,
        tool_name: str = "",
        code: str = "",
        raw: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.tool_name = tool_name
        self.code = code
        self.raw = raw or {}


class EntityNotFound(MCPToolError):
    """umodel entity does not exist in the workspace/domain."""


class AuthError(MCPToolError):
    """AK/SK invalid, expired, or lacks permission for the tool."""


class QueryError(MCPToolError):
    """SPL/SQL/PromQL syntax error or query semantic violation."""
