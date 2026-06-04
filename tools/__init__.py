"""
AgenticSRE Tools Package
Exports all tool classes and provides the build_tool_registry() factory.
"""

from tools.base_tool import SRETool, ToolResult, ToolRegistry
from tools.llm_client import LLMClient
from tools.k8s_tools import KubectlTool, K8sResourceTool, K8sHealthTool
from tools.observability import PrometheusTool, ElasticsearchTool, JaegerTool
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
    "PrometheusTool", "ElasticsearchTool", "JaegerTool",
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
    
    This is the single factory function that wires everything together.
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

    # ── Observability Tools ──
    registry.register(
        PrometheusTool(cfg.observability.prometheus_url, llm_client=llm),
        "observability"
    )
    registry.register(
        ElasticsearchTool(cfg.observability.elasticsearch_url),
        "observability"
    )
    registry.register(
        JaegerTool(cfg.observability.jaeger_url),
        "observability"
    )

    # ── Analysis Tools ──
    registry.register(AnomalyDetectionTool(), "analysis")
    registry.register(RCALocalizationTool(), "analysis")

    return registry
