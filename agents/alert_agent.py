"""
AgenticSRE Alert Agent
告警压缩与根因推荐智能体 — SOW核心交付项
Implements semantic alert compression, temporal-spatial correlation,
and LLM-based root cause recommendation.
"""

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, field

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    """Structured alert representation."""
    alert_id: str = ""
    name: str = ""
    severity: str = "warning"   # critical | warning | info
    source: str = ""            # prometheus | k8s_event | es_log
    timestamp: float = 0.0
    labels: Dict[str, str] = field(default_factory=dict)
    message: str = ""
    fingerprint: str = ""
    group_id: str = ""          # After compression

    def __post_init__(self):
        if not self.fingerprint:
            content = f"{self.name}:{self.source}:{sorted(self.labels.items())}"
            self.fingerprint = hashlib.md5(content.encode()).hexdigest()[:12]
        if not self.alert_id:
            self.alert_id = f"alert-{self.fingerprint}-{int(self.timestamp)}"


@dataclass
class AlertGroup:
    """Compressed alert group — multiple alerts collapsed to one."""
    group_id: str
    representative: Alert            # Most informative alert in group
    alerts: List[Alert] = field(default_factory=list)
    root_cause_suggestion: str = ""
    confidence: float = 0.0
    affected_services: List[str] = field(default_factory=list)

    @property
    def compression_ratio(self) -> float:
        return len(self.alerts) / 1.0 if self.alerts else 0

    def summary(self) -> Dict:
        return {
            "group_id": self.group_id,
            "alert_count": len(self.alerts),
            "representative": self.representative.name,
            "severity": max((a.severity for a in self.alerts), key=lambda s: {"critical": 3, "warning": 2, "info": 1}.get(s, 0)),
            "root_cause": self.root_cause_suggestion,
            "confidence": self.confidence,
            "affected_services": self.affected_services,
            "time_range": f"{min(a.timestamp for a in self.alerts):.0f} - {max(a.timestamp for a in self.alerts):.0f}",
        }


class AlertAgent:
    """
    Alert compression and root cause recommendation agent.
    
    SOW Requirement: "告警压缩与根因推荐智能体 — 根因推荐准确率≥80%"
    
    Approach:
    1. Temporal grouping — alerts within time_window are candidates
    2. Spatial grouping — alerts on same service/node/namespace
    3. Semantic similarity — LLM-based semantic correlation
    4. Root cause recommendation — LLM analyzes compressed groups
    """

    COMPRESSION_PROMPT = """You are an expert SRE alert analyst. Given a list of alerts, group them by root cause.
For each group:
1. Identify alerts caused by the same underlying issue
2. Select the most representative alert
3. Suggest the root cause
4. Rate your confidence (0.0-1.0)

Input alerts:
{alerts}

Respond in JSON format:
{{
    "groups": [
        {{
            "group_name": "descriptive name",
            "alert_indices": [0, 2, 5],
            "root_cause": "explanation of likely root cause",
            "confidence": 0.85,
            "affected_services": ["svc1", "svc2"]
        }}
    ],
    "compression_summary": "brief summary of compression results"
}}"""

    RCA_PROMPT = """You are an expert SRE root cause analyst. Given a compressed alert group, 
provide a detailed root cause analysis.

Alert Group: {group_name}
Alerts ({count}):
{alert_details}

Cluster Context:
{cluster_context}

Analyze the root cause and provide:
1. Most likely root cause (be specific)
2. Evidence supporting this conclusion
3. Recommended investigation steps
4. Suggested remediation

Respond in JSON:
{{
    "root_cause": "specific root cause explanation",
    "evidence": ["evidence 1", "evidence 2"],
    "investigation_steps": ["step 1", "step 2"],
    "remediation": "recommended fix",
    "confidence": 0.85,
    "severity": "critical|high|medium|low"
}}"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, config=None):
        self.llm = llm
        self.registry = registry
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.offline_mode = bool(
            getattr(cfg.observability, "offline_mode", False)
            and getattr(cfg.observability, "backend", "") == "alidata"
        )
        self.time_window = cfg.alert.time_window
        self.similarity_threshold = cfg.alert.similarity_threshold
        self.max_group_size = cfg.alert.max_group_size

    async def compress_and_recommend(self, alerts: List[Alert] = None,
                                      namespace: str = "") -> Dict:
        """Main entry: fetch alerts, compress, and recommend root causes."""
        
        # Phase 1: Collect alerts from all sources
        if alerts is None:
            alerts = await self._collect_alerts(namespace)
        
        if not alerts:
            return {
                "agent": "alert_agent",
                "total_alerts": 0,
                "groups": [],
                "compression_ratio": 0,
                "summary": "No alerts found",
            }

        # Phase 2: Temporal-spatial pre-grouping
        pre_groups = self._temporal_spatial_group(alerts)
        
        # Phase 3: LLM semantic compression
        final_groups = await self._semantic_compress(pre_groups, alerts)
        
        # Phase 4: Root cause recommendation per group
        for group in final_groups:
            rca = await self._recommend_root_cause(group)
            group.root_cause_suggestion = rca.get("root_cause", "")
            group.confidence = rca.get("confidence", 0.0)

        # Compute compression statistics
        total = len(alerts)
        groups_count = len(final_groups)
        compression_ratio = 1 - (groups_count / total) if total > 0 else 0

        return {
            "agent": "alert_agent",
            "total_alerts": total,
            "compressed_groups": groups_count,
            "compression_ratio": round(compression_ratio, 3),
            "groups": [g.summary() for g in final_groups],
            "summary": f"Compressed {total} alerts into {groups_count} groups "
                       f"(compression ratio: {compression_ratio:.1%})",
        }

    async def _collect_alerts(self, namespace: str = "") -> List[Alert]:
        """Collect alerts from Prometheus, K8s events, and ES."""
        alerts = []
        if self.offline_mode:
            from agents.detection_agent import DetectionAgent

            detector = DetectionAgent(self.llm, self.registry)
            signals = detector.detect(namespace)
            for signal in signals:
                alerts.append(Alert(
                    name=signal.title,
                    severity=signal.severity,
                    source=signal.source,
                    timestamp=signal.timestamp,
                    labels={
                        "namespace": signal.namespace,
                        "service": signal.service,
                    },
                    message=signal.description,
                ))
            return alerts
        
        # Source 1: Prometheus alerts
        prom = self.registry.get("prometheus")
        if prom:
            result = prom.execute(query="ALERTS{alertstate='firing'}")
            if result.success and result.data:
                for item in result.data.get("results", []):
                    metric = item.get("metric", {})
                    alerts.append(Alert(
                        name=metric.get("alertname", "unknown"),
                        severity=metric.get("severity", "warning"),
                        source="prometheus",
                        timestamp=time.time(),
                        labels=metric,
                        message=metric.get("description", metric.get("summary", "")),
                    ))
        
        # Source 2: K8s Warning events
        kubectl = self.registry.get("kubectl")
        if kubectl:
            result = kubectl.execute(
                command="get events --field-selector type=Warning --sort-by='.lastTimestamp' -o json",
                namespace=namespace,
            )
            if result.success and result.data:
                import json
                try:
                    data = json.loads(result.data) if isinstance(result.data, str) else result.data
                    for event in data.get("items", [])[:50]:
                        obj = event.get("involvedObject", {})
                        alerts.append(Alert(
                            name=event.get("reason", "K8sWarning"),
                            severity="warning",
                            source="k8s_event",
                            timestamp=time.time(),
                            labels={
                                "kind": obj.get("kind", ""),
                                "name": obj.get("name", ""),
                                "namespace": obj.get("namespace", namespace),
                            },
                            message=event.get("message", "")[:500],
                        ))
                except (json.JSONDecodeError, TypeError):
                    pass

        return alerts

    def _temporal_spatial_group(self, alerts: List[Alert]) -> Dict[str, List[int]]:
        """Pre-group alerts by time window + spatial locality."""
        groups = defaultdict(list)
        
        for i, alert in enumerate(alerts):
            # Group key: source + node/service + time bucket
            svc = (alert.labels.get("service", "") or 
                   alert.labels.get("pod", "") or 
                   alert.labels.get("name", "") or
                   alert.labels.get("instance", ""))
            ns = alert.labels.get("namespace", "")
            time_bucket = int(alert.timestamp // self.time_window)
            key = f"{alert.source}:{ns}:{svc}:{time_bucket}"
            groups[key].append(i)
        
        return dict(groups)

    async def _semantic_compress(self, pre_groups: Dict[str, List[int]],
                                   alerts: List[Alert]) -> List[AlertGroup]:
        """Use LLM to semantically compress pre-grouped alerts."""
        # If few enough alerts, use LLM to do global compression
        if len(alerts) <= 50:
            return await self._llm_compress(alerts)
        
        # Otherwise, compress each pre-group separately
        final_groups = []
        for key, indices in pre_groups.items():
            group_alerts = [alerts[i] for i in indices]
            if len(group_alerts) == 1:
                final_groups.append(AlertGroup(
                    group_id=f"grp-{key[:8]}",
                    representative=group_alerts[0],
                    alerts=group_alerts,
                ))
            else:
                sub_groups = await self._llm_compress(group_alerts)
                final_groups.extend(sub_groups)
        
        return final_groups

    async def _llm_compress(self, alerts: List[Alert]) -> List[AlertGroup]:
        """Use LLM for semantic alert compression."""
        alert_text = "\n".join([
            f"[{i}] name={a.name} severity={a.severity} source={a.source} "
            f"labels={a.labels} message={a.message[:200]}"
            for i, a in enumerate(alerts)
        ])
        
        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "You are an SRE alert compression expert."},
                {"role": "user", "content": self.COMPRESSION_PROMPT.format(alerts=alert_text[:6000])}
            ])
            
            groups = []
            for g in result.get("groups", []):
                indices = g.get("alert_indices", [])
                group_alerts = [alerts[i] for i in indices if i < len(alerts)]
                if group_alerts:
                    groups.append(AlertGroup(
                        group_id=f"grp-{hashlib.md5(g.get('group_name', '').encode()).hexdigest()[:8]}",
                        representative=group_alerts[0],
                        alerts=group_alerts,
                        root_cause_suggestion=g.get("root_cause", ""),
                        confidence=g.get("confidence", 0.0),
                        affected_services=g.get("affected_services", []),
                    ))
            return groups
            
        except Exception as e:
            logger.error(f"LLM compression failed: {e}")
            # Fallback: one group per alert
            return [
                AlertGroup(group_id=f"grp-{a.fingerprint}", representative=a, alerts=[a])
                for a in alerts
            ]

    async def _recommend_root_cause(self, group: AlertGroup) -> Dict:
        """Recommend root cause for an alert group."""
        alert_details = "\n".join([
            f"- [{a.severity}] {a.name}: {a.message[:200]} (source: {a.source})"
            for a in group.alerts[:20]
        ])
        
        # Get cluster context
        cluster_context = ""
        k8s_health = self.registry.get("k8s_health")
        if k8s_health:
            result = k8s_health.execute(component="pods")
            if result.success:
                cluster_context = str(result.data)[:2000]

        try:
            return self.llm.json_chat([
                {"role": "system", "content": "You are an SRE root cause analyst."},
                {"role": "user", "content": self.RCA_PROMPT.format(
                    group_name=group.representative.name,
                    count=len(group.alerts),
                    alert_details=alert_details,
                    cluster_context=cluster_context[:2000],
                )}
            ])
        except Exception as e:
            logger.error(f"RCA recommendation failed: {e}")
            return {"root_cause": "Analysis failed", "confidence": 0.0}
