"""
AgenticSRE Profiling Agent
System profiling and performance analysis agent.
SOW Requirement: "构建专用智能体（比如...Profiling...）"
"""

import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


class ProfilingAgent:
    """
    Profiling agent for deep performance analysis.
    Analyzes CPU/memory/IO profiles of containers and nodes.
    """

    SYSTEM_PROMPT = """You are a system performance profiling expert.
Analyze the provided resource usage data and identify performance bottlenecks.
Focus on:
1. CPU throttling and saturation
2. Memory pressure and OOM risks
3. I/O bottlenecks and disk saturation
4. Network bandwidth and latency issues
5. Container resource limit violations

Provide specific recommendations for optimization."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry

    async def analyze(self, query: str, namespace: str = "",
                      target_pod: str = "") -> Dict:
        """Run profiling analysis."""
        results = {}
        
        # Node-level metrics
        results["node_resources"] = self._get_node_resources()
        
        # Pod-level metrics
        results["pod_resources"] = self._get_pod_resources(namespace, target_pod)
        
        # Container resource limits vs usage
        results["resource_limits"] = self._check_resource_limits(namespace, target_pod)
        
        # Prometheus deep metrics
        results["prometheus_profile"] = self._prometheus_deep_metrics(namespace, target_pod)

        # LLM analysis
        context = f"Profiling query: {query}\n\n"
        for key, data in results.items():
            context += f"\n[{key}]:\n{str(data)[:1500]}\n"

        summary = self.llm.chat([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": context[:8000]}
        ])

        return {
            "agent": "profiling_agent",
            "query": query,
            "results": results,
            "summary": summary,
        }

    def _get_node_resources(self) -> Optional[str]:
        kubectl = self.registry.get("kubectl")
        if not kubectl:
            return None
        result = kubectl.execute(command="top nodes")
        return result.data if result.success else result.error

    def _get_pod_resources(self, namespace: str, pod: str) -> Optional[str]:
        kubectl = self.registry.get("kubectl")
        if not kubectl:
            return None
        cmd = "top pods"
        if pod:
            cmd += f" {pod}"
        result = kubectl.execute(command=cmd, namespace=namespace or "default")
        return result.data if result.success else result.error

    def _check_resource_limits(self, namespace: str, pod: str) -> Optional[Dict]:
        kubectl = self.registry.get("kubectl")
        if not kubectl:
            return None
        cmd = "get pods -o json"
        if pod:
            cmd = f"get pod {pod} -o json"
        result = kubectl.execute(command=cmd, namespace=namespace or "default")
        if not result.success:
            return None
        
        import json
        try:
            data = json.loads(result.data)
            pods = data.get("items", [data]) if "items" in data else [data]
            limit_info = []
            for p in pods[:10]:
                for c in p.get("spec", {}).get("containers", []):
                    limits = c.get("resources", {}).get("limits", {})
                    requests = c.get("resources", {}).get("requests", {})
                    if limits or requests:
                        limit_info.append({
                            "pod": p.get("metadata", {}).get("name", ""),
                            "container": c.get("name", ""),
                            "limits": limits,
                            "requests": requests,
                        })
            return limit_info
        except (json.JSONDecodeError, TypeError):
            return None

    def _prometheus_deep_metrics(self, namespace: str, pod: str) -> Dict:
        prom = self.registry.get("prometheus")
        if not prom:
            return {}
        
        queries = {
            "cpu_throttle": 'rate(container_cpu_cfs_throttled_seconds_total[5m])',
            "memory_working_set": 'container_memory_working_set_bytes',
            "network_receive": 'rate(container_network_receive_bytes_total[5m])',
            "network_transmit": 'rate(container_network_transmit_bytes_total[5m])',
            "disk_io_read": 'rate(container_fs_reads_total[5m])',
            "disk_io_write": 'rate(container_fs_writes_total[5m])',
        }
        
        results = {}
        for name, query in queries.items():
            if pod:
                query += f'{{pod="{pod}"}}'
            elif namespace:
                query += f'{{namespace="{namespace}"}}'
            
            result = prom.execute(query=query, query_type="instant")
            if result.success:
                results[name] = result.data
        
        return results
