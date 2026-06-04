"""
AgenticSRE Trace Agent
Fetches and analyzes distributed traces from Jaeger with Hero latency analysis.
"""

import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient
from tools.hero_analysis import HeroTraceAnalyzer

logger = logging.getLogger(__name__)


class TraceAgent:
    """
    Trace analysis agent: fetches traces from Jaeger, runs Hero latency
    analysis + WeRCA time-window comparison, identifies slow spans and
    service bottlenecks.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE distributed tracing expert.
Analyze the provided trace data and latency statistics.
Identify:
1. Services with abnormal latency (p95/p99 violations)
2. Bottleneck spans in the request chain
3. Latency degradation compared to baseline
4. Service-to-service call patterns that indicate issues
5. Error spans and their propagation

Be precise about latency values and service names. Format as structured analysis."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.analyzer = HeroTraceAnalyzer()
        self.summary_max_tokens = 1024

    async def analyze(self, query: str, service: str = "",
                      namespace: str = "") -> Dict:
        """Run trace analysis for the given incident query."""
        
        # Get available services
        services = self._list_services()
        
        # Fetch traces for specified or all services
        all_traces = {}
        target_services = [service] if service else (services or [])
        
        for svc in target_services[:10]:  # Limit
            traces = self._fetch_traces(svc)
            if traces:
                all_traces[svc] = traces

        # Run Hero latency analysis per service
        latency_results = {}
        for svc, traces in all_traces.items():
            analysis = self.analyzer.latency_analysis(traces)
            if analysis and analysis.get("count", 0) > 0:
                latency_results[svc] = analysis

        # LLM summarization
        context = f"Incident: {query}\n\n"
        context += f"Services analyzed: {len(all_traces)}\n"
        for svc, result in latency_results.items():
            context += f"\n[{svc}]:\n"
            context += f"  Traces: {result.get('count', 0)}\n"
            context += f"  Mean: {result.get('mean_us', 0)}μs\n"
            context += f"  P95: {result.get('p95_us', 0)}μs\n"
            context += f"  P99: {result.get('p99_us', 0)}μs\n"
            context += f"  Slow traces: {result.get('slow_trace_count', 0)}\n"

        summary = await self.llm.async_chat([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": context[:8000]}
        ], max_tokens=self.summary_max_tokens)

        return {
            "agent": "trace_agent",
            "query": query,
            "services_analyzed": len(all_traces),
            "latency_results": latency_results,
            "summary": summary,
        }

    def _list_services(self) -> List[str]:
        """List available services in Jaeger."""
        jaeger = self.registry.get("jaeger")
        if jaeger is None:
            return []
        result = jaeger.execute()
        if result.success and isinstance(result.data, dict):
            return result.data.get("data", [])
        return []

    def _fetch_traces(self, service: str) -> List[Dict]:
        """Fetch traces for a service."""
        jaeger = self.registry.get("jaeger")
        if jaeger is None:
            return []
        result = jaeger.execute(service=service, limit=50, lookback="1h")
        if result.success and isinstance(result.data, dict):
            return result.data.get("traces", [])
        return []
