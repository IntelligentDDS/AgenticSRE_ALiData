"""
AgenticSRE Log Agent
Fetches and analyzes logs from Elasticsearch with Hero pattern analysis and Drain3 clustering.
"""

import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient
from tools.hero_analysis import HeroLogAnalyzer

logger = logging.getLogger(__name__)


class LogAgent:
    """
    Log analysis agent: fetches logs from Elasticsearch, applies Hero pattern
    analysis + Drain3 clustering, identifies error patterns, and produces
    LLM-summarized log insights.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE log analysis expert.
Analyze the provided log data, patterns, and clusters.
Identify:
1. Error patterns and their frequency
2. Anomalous log entries (rare patterns)
3. Root cause indicators from log messages
4. Timeline of error propagation
5. Affected components/pods/services

Be specific about error messages and their implications. Format as structured analysis."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.analyzer = HeroLogAnalyzer()
        self.summary_max_tokens = 1024

    async def analyze(self, query: str, namespace: str = "",
                      time_range: str = "1h", pod_name: str = "") -> Dict:
        """Run log analysis for the given incident query."""
        
        # Strategy 1: Elasticsearch search
        es_results = self._search_elasticsearch(query, namespace, time_range)
        
        # Strategy 2: Direct pod logs via kubectl
        kubectl_logs = {}
        if pod_name:
            kubectl_logs = self._fetch_pod_logs(pod_name, namespace)
        
        # Combine all log entries
        all_entries = []
        if es_results:
            all_entries.extend([
                entry.get("message", "") for entry in es_results.get("entries", [])
            ])
        if kubectl_logs:
            all_entries.extend(kubectl_logs.get("lines", []))

        # Run analysis
        pattern_result = self.analyzer.pattern_analysis(all_entries) if all_entries else {}
        cluster_result = self.analyzer.drain3_cluster(all_entries) if len(all_entries) > 10 else {}

        # LLM summarization
        context = f"Incident: {query}\n\n"
        context += f"Total log entries: {len(all_entries)}\n"
        if pattern_result:
            context += f"\nPattern Analysis:\n"
            context += f"- Unique patterns: {pattern_result.get('unique_patterns', 0)}\n"
            context += f"- Error count: {pattern_result.get('error_count', 0)}\n"
            context += f"- Error samples: {pattern_result.get('error_samples', [])}\n"
            context += f"- Rare patterns: {pattern_result.get('rare_patterns', [])}\n"
        if cluster_result:
            context += f"\nLog Clusters:\n"
            for c in cluster_result.get("clusters", [])[:10]:
                context += f"  [{c.get('size', 0)}x] {c.get('template', '')[:100]}\n"

        summary = await self.llm.async_chat([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": context[:8000]}
        ], max_tokens=self.summary_max_tokens)

        return {
            "agent": "log_agent",
            "query": query,
            "total_entries": len(all_entries),
            "pattern_analysis": pattern_result,
            "cluster_analysis": cluster_result,
            "summary": summary,
        }

    def _search_elasticsearch(self, query: str, namespace: str, time_range: str) -> Optional[Dict]:
        """Search Elasticsearch for logs."""
        es = self.registry.get("elasticsearch")
        if es is None:
            return None
        # Search for errors first, then general query
        result = es.execute(query=query, time_range=time_range, namespace=namespace, level="error")
        if result.success and result.data:
            return result.data
        # Fallback: general search
        result = es.execute(query=query, time_range=time_range, namespace=namespace)
        return result.data if result.success else None

    def _fetch_pod_logs(self, pod_name: str, namespace: str) -> Optional[Dict]:
        """Fetch logs directly from a pod via kubectl."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return None
        result = kubectl.execute(command=f"logs {pod_name} --tail=500", namespace=namespace)
        if result.success and result.data:
            lines = result.data.strip().split("\n")
            return {"pod": pod_name, "lines": lines, "count": len(lines)}
        return None
