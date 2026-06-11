"""
AgenticSRE Tools Package
Exports all tool classes and provides the build_tool_registry() factory.
"""

from tools.base_tool import SRETool, ToolResult, ToolRegistry
from tools.llm_client import LLMClient
from tools.k8s_tools import KubectlTool, K8sResourceTool, K8sHealthTool
from tools.mcp_observability import MCPMetricTool, MCPLogTool, MCPTraceTool
from tools.mcp_client import MCPClient
from tools.anomaly_detection import AnomalyDetectionTool
from tools.hero_analysis import (
    HeroMetricAnalyzer, HeroLogAnalyzer,
    HeroTraceAnalyzer, HeroCrossSignalCorrelator,
)
from tools.rca_localization import RCALocalizationTool
from tools.action_stack import ActionStack, Action

__all__ = [
    "SRETool", "ToolResult", "ToolRegistry",
    "LLMClient",
    "KubectlTool", "K8sResourceTool", "K8sHealthTool",
    "MCPClient", "MCPMetricTool", "MCPLogTool", "MCPTraceTool",
    "AnomalyDetectionTool",
    "HeroMetricAnalyzer", "HeroLogAnalyzer",
    "HeroTraceAnalyzer", "HeroCrossSignalCorrelator",
    "RCALocalizationTool",
    "ActionStack", "Action",
    "build_tool_registry",
]


def build_tool_registry(config=None, allow_write: bool = False) -> ToolRegistry:
    """
    Build and populate the global ToolRegistry with all tools.

    Wires LLM, K8s, observability (via MCP backend), and analysis tools.
    """
    from configs.config_loader import get_config
    cfg = config or get_config()

    registry = ToolRegistry.get_instance()
    registry.reset()  # Clean slate

    # ── LLM Client ──
    llm = LLMClient(cfg.llm)

    # ── K8s Tools ──
    kubectl = KubectlTool(
        kubeconfig=cfg.kubernetes.kubeconfig,
        namespace=cfg.kubernetes.namespace,
        allow_write=allow_write or cfg.runtime.enable_self_healing,
        use_dry_run=cfg.kubernetes.use_dry_run,
        forbidden_commands=cfg.kubernetes.forbid_unsafe_commands,
        ssh_jump_host=cfg.kubernetes.ssh_jump_host,
        target_host=cfg.kubernetes.target_host,
        use_ssh=cfg.kubernetes.use_ssh,
    )
    registry.register(kubectl, "kubernetes")
    registry.register(K8sResourceTool(kubectl), "kubernetes")
    registry.register(K8sHealthTool(kubectl), "kubernetes")

    # ── Observability Tools (MCP backend) ──
    backend = getattr(cfg.observability, "backend", "mcp")
    if backend != "mcp":
        raise ValueError(
            f"observability.backend='{backend}' is not supported; "
            "AgenticSRE_MCP only supports backend='mcp'"
        )

    mcp_client = MCPClient(
        endpoint=getattr(cfg.observability, "mcp_endpoint",
                         "http://localhost:7980/mcp"),
        timeout=float(getattr(cfg.observability, "mcp_timeout_seconds", 60)),
        retries=int(getattr(cfg.observability, "mcp_transport_retry", 3)),
    )

    region = getattr(cfg.observability, "default_region", "cn-hongkong")
    workspace = getattr(cfg.observability, "default_workspace", "")
    domain = getattr(cfg.observability, "default_domain", "apm")
    entity_set = getattr(cfg.observability, "default_entity_set", "apm.service")
    log_set_domain = getattr(cfg.observability, "default_log_set_domain", "apm")
    log_set_name = getattr(cfg.observability, "default_log_set_name", "")
    trace_set_domain = getattr(cfg.observability, "default_trace_set_domain", "apm")
    trace_set_name = getattr(cfg.observability, "default_trace_set_name", "apm.trace.common")

    registry.register(
        MCPMetricTool(
            mcp_client,
            default_region=region, default_workspace=workspace,
            default_domain=domain, default_entity_set=entity_set,
        ),
        "observability",
    )
    registry.register(
        MCPLogTool(
            mcp_client,
            default_region=region, default_workspace=workspace,
            default_domain=domain, default_entity_set=entity_set,
            default_log_set_domain=log_set_domain,
            default_log_set_name=log_set_name,
        ),
        "observability",
    )
    registry.register(
        MCPTraceTool(
            mcp_client,
            default_region=region, default_workspace=workspace,
            default_domain=domain, default_entity_set=entity_set,
            default_trace_set_domain=trace_set_domain,
            default_trace_set_name=trace_set_name,
        ),
        "observability",
    )

    # ── Analysis Tools ──
    registry.register(AnomalyDetectionTool(), "analysis")
    registry.register(RCALocalizationTool(), "analysis")

    return registry
