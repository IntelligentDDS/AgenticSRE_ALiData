"""Bootstrap MCP backend: instantiate MCPClient, register adapters."""
from __future__ import annotations

import os
import logging
from typing import Any, Dict

from .base_tool import ToolRegistry
from .mcp_client import MCPClient
from .mcp_observability import MCPMetricTool, MCPLogTool, MCPTraceTool

logger = logging.getLogger(__name__)


def init(config: Dict[str, Any]) -> MCPClient:
    """Initialize MCP backend from config. Returns the shared MCPClient.

    Args:
        config: parsed configs/config.yaml as dict.

    Raises:
        ValueError: if observability.backend is not "mcp".
    """
    obs = config.get("observability") or {}
    backend = obs.get("backend", "").lower()
    if backend != "mcp":
        raise ValueError(
            f"mcp_bootstrap.init() requires observability.backend='mcp', got '{backend}'"
        )

    endpoint = obs.get("mcp_endpoint") or "http://localhost:7980/mcp"
    timeout = float(obs.get("mcp_timeout_seconds") or 60)
    retries = int(obs.get("mcp_transport_retry") or 3)

    default_region = obs.get("default_region") or os.environ.get("REGION", "cn-hongkong")
    default_workspace = obs.get("default_workspace") or os.environ.get("WORKSPACE_NAME", "")
    default_domain = obs.get("default_domain") or "apm"
    default_entity_set = obs.get("default_entity_set") or "apm.service"
    default_log_set_domain = obs.get("default_log_set_domain") or "apm"
    default_log_set_name = obs.get("default_log_set_name") or ""
    default_trace_set_domain = obs.get("default_trace_set_domain") or "apm"
    default_trace_set_name = obs.get("default_trace_set_name") or "apm.trace.common"

    client = MCPClient(endpoint=endpoint, timeout=timeout, retries=retries)
    logger.info("MCPClient initialized: endpoint=%s timeout=%s retries=%s",
                endpoint, timeout, retries)

    registry = ToolRegistry()
    registry.register(
        MCPMetricTool(
            client,
            default_region=default_region,
            default_workspace=default_workspace,
            default_domain=default_domain,
            default_entity_set=default_entity_set,
        ),
        category="observability",
    )
    registry.register(
        MCPLogTool(
            client,
            default_region=default_region,
            default_workspace=default_workspace,
            default_domain=default_domain,
            default_entity_set=default_entity_set,
            default_log_set_domain=default_log_set_domain,
            default_log_set_name=default_log_set_name,
        ),
        category="observability",
    )
    registry.register(
        MCPTraceTool(
            client,
            default_region=default_region,
            default_workspace=default_workspace,
            default_domain=default_domain,
            default_entity_set=default_entity_set,
            default_trace_set_domain=default_trace_set_domain,
            default_trace_set_name=default_trace_set_name,
        ),
        category="observability",
    )
    logger.info("Registered MCP tools: prometheus, elasticsearch, jaeger")
    return client
