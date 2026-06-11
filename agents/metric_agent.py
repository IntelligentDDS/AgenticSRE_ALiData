"""
AgenticSRE Metric Agent
Fetches and analyzes Prometheus metrics with Hero 3σ + WeRCA onset detection.
"""

import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient
from tools.hero_analysis import HeroMetricAnalyzer

logger = logging.getLogger(__name__)


class MetricAgent:
    """
    Metric analysis agent: fetches Prometheus metrics, runs statistical
    anomaly detection (Hero 3σ + WeRCA Pearson onset), and produces
    LLM-summarized metric insights.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE metric analysis expert.
Analyze the provided Prometheus metric data and anomaly detection results.
Identify:
1. Which metrics show anomalous behavior
2. The severity and timing of anomalies
3. Correlation with potential infrastructure or application issues
4. Specific services/pods affected

Be precise and cite specific metric values. Format as structured analysis."""

    # Key metric queries for different fault categories
    METRIC_QUERIES = {
        "infrastructure": [
            ('node_cpu_usage', 'avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance) * 100'),
            ('node_memory_usage', '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100'),
            ('node_disk_usage', '(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100'),
            ('node_network_errors', 'rate(node_network_receive_errs_total[5m])'),
        ],
        "application": [
            ('container_cpu', 'sum(rate(container_cpu_usage_seconds_total[5m])) by (pod, namespace)'),
            ('container_memory', 'container_memory_working_set_bytes / container_spec_memory_limit_bytes * 100'),
            ('container_restarts', 'increase(kube_pod_container_status_restarts_total[1h])'),
            ('pod_not_ready', 'kube_pod_status_ready{condition="false"}'),
        ],
        "workload": [
            ('http_request_rate', 'sum(rate(http_requests_total[5m])) by (service)'),
            ('http_error_rate', 'sum(rate(http_requests_total{code=~"5.."}[5m])) by (service)'),
            ('http_latency_p99', 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))'),
        ],
    }

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.hero = HeroMetricAnalyzer()
        self.summary_max_tokens = 1024

    async def analyze(self, query: str, namespace: str = "",
                      categories: Optional[List[str]] = None) -> Dict:
        """Run metric analysis for the given incident query."""
        categories = categories or ["infrastructure", "application", "workload"]
        
        all_results = {}
        all_anomalies = {}
        
        for category in categories:
            queries = self.METRIC_QUERIES.get(category, [])
            for metric_name, promql in queries:
                result = self._fetch_metric(promql, namespace)
                if result and result.get("results"):
                    all_results[metric_name] = result
                    
                    # Run Hero 3σ anomaly detection on each series
                    for series in result.get("results", []):
                        values = self._extract_values(series)
                        if values:
                            anomaly = self.hero.three_sigma_detect(values)
                            if anomaly.get("anomaly_count", 0) > 0:
                                label = series.get("metric", {})
                                service = (label.get("pod", "") or 
                                         label.get("instance", "") or 
                                         label.get("service", metric_name))
                                all_anomalies[f"{metric_name}:{service}"] = anomaly
                            
                            # WeRCA onset detection
                            onset = self.hero.pearson_onset_detection(values)
                            if onset.get("onset_points"):
                                all_anomalies[f"{metric_name}:{service}:onset"] = onset

        # LLM summarization
        context = f"Incident: {query}\n\nMetric Analysis Results:\n"
        for name, data in all_results.items():
            context += f"\n[{name}]: {len(data.get('results', []))} series"
        context += f"\n\nAnomalies Detected ({len(all_anomalies)}):\n"
        for name, anomaly in all_anomalies.items():
            context += f"\n[{name}]: {anomaly}"

        summary = await self.llm.async_chat([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": context[:8000]}
        ], max_tokens=self.summary_max_tokens)

        return {
            "agent": "metric_agent",
            "query": query,
            "metrics_fetched": len(all_results),
            "anomalies_found": len(all_anomalies),
            "anomaly_details": all_anomalies,
            "summary": summary,
        }

    def _fetch_metric(self, promql: str, namespace: str = "") -> Optional[Dict]:
        """Fetch metric range from MCP-backed Prometheus tool — need
        multi-point time series for 3σ anomaly detection."""
        prom = self.registry.get("prometheus")
        if prom is None:
            return None
        import time as _t
        now = int(_t.time())
        result = prom.execute(
            query=promql,
            query_type="range",
            start=str(now - 1800),  # 30-min window
            end=str(now),
            step="60s",
        )
        return result.data if result.success else None

    def _extract_values(self, series: Dict) -> List[float]:
        """Extract float values from a Prometheus series result."""
        if "values" in series:
            return [float(v[1]) for v in series["values"] if v[1] != "NaN"]
        elif "value" in series:
            try:
                return [float(series["value"][1])]
            except (IndexError, ValueError):
                return []
        return []
