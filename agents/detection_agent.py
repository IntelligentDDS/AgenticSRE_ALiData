"""
AgenticSRE Detection Agent
Continuous anomaly detection: polls Prometheus alerts, K8s events, ES errors, metric anomalies.
Phase 1 of the 5-phase pipeline.
"""

import time
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from tools.base_tool import ToolRegistry
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


def _fetch_cms_alerts_via_mcp(limit: int = 500, lookback: str = "now-1h") -> List[Dict]:
    """Pull real CMS alert events from SLS via the MCP client.

    Workspace: rca-benchmark
    Logstore: cms-event-store-rca-benchmark

    SLS tool caps each call at 100 rows, so we paginate via SQL OFFSET to
    pull up to `limit` rows. Then dedupe by (subject, severity, resource)
    so repeated triggers of the same alert collapse to one entry.
    """
    import json as _json
    logger = __import__("logging").getLogger(__name__)
    try:
        from tools import build_tool_registry
        reg = build_tool_registry()
        prom = reg.get("prometheus")
        if prom is None or prom.client is None:
            return []

        all_rows = []
        page_size = 100
        # Use ORDER BY time DESC so we get latest alerts first
        pages_needed = max(1, (limit + page_size - 1) // page_size)
        for page in range(pages_needed):
            offset = page * page_size
            try:
                raw = prom.client.call_tool("sls_execute_sql", {
                    "regionId": "cn-hongkong",
                    "project": "cms-alert-center-1819385687343877-cn-hongkong",
                    "logStore": "cms-event-store-rca-benchmark",
                    "query": f"type:ALERT | select * order by __time__ desc limit {offset}, {page_size}",
                    "from_time": lookback,
                    "to_time": "now",
                    "limit": page_size,
                })
            except Exception as exc:
                logger.debug("page %d failed: %s", page, exc)
                break
            rows = raw.get("data") or raw.get("results") or []
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < page_size:
                break  # last page

        # Dedupe by (subject, severity, resource)
        seen = {}
        for r in all_rows:
            if not isinstance(r, dict):
                continue
            labels = r.get("labels", "{}")
            if isinstance(labels, str):
                try: labels = _json.loads(labels)
                except Exception: labels = {}
            annotations = r.get("annotations", "{}")
            if isinstance(annotations, str):
                try: annotations = _json.loads(annotations)
                except Exception: annotations = {}
            subj = r.get("subject", "")
            sev = (r.get("severity") or "").lower() or "warning"
            res = r.get("resource", "")
            key = (subj, sev, res[:200])  # truncate resource for dedup key
            if key in seen:
                # Bump count, keep latest time
                seen[key]["count"] += 1
                continue
            seen[key] = {
                "id": r.get("id") or "",
                "subject": subj,
                "severity": sev,
                "status": r.get("status", ""),
                "subtype": r.get("subtype", ""),
                "time": r.get("time", ""),
                "labels": labels if isinstance(labels, dict) else {},
                "annotations": annotations if isinstance(annotations, dict) else {},
                "resource": res,
                "count": 1,
            }
        out = list(seen.values())
        logger.info("CMS alerts: %d raw -> %d unique", len(all_rows), len(out))
        return out
    except Exception as exc:
        logger.warning("CMS alert fetch failed: %s", exc)
        return []



@dataclass
class DetectionSignal:
    """A detected anomaly signal that may trigger the pipeline."""
    signal_id: str
    source: str             # prometheus | k8s_event | es_log | metric_detector
    severity: str           # critical | warning | info
    title: str
    description: str
    timestamp: float = 0.0
    namespace: str = ""
    service: str = ""
    fingerprint: str = ""
    raw_data: Any = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.fingerprint:
            content = f"{self.source}:{self.title}:{self.service}:{self.namespace}"
            self.fingerprint = hashlib.md5(content.encode()).hexdigest()[:12]
        if not self.signal_id:
            self.signal_id = f"sig-{self.fingerprint}-{int(self.timestamp)}"

    def to_dict(self) -> Dict:
        return {
            "signal_id": self.signal_id,
            "source": self.source,
            "severity": self.severity,
            "title": self.title,
            "description": self.description[:500],
            "timestamp": self.timestamp,
            "namespace": self.namespace,
            "service": self.service,
        }


class DetectionAgent:
    """
    Continuous anomaly detection agent.
    Polls multiple signal sources and produces DetectionSignals.
    """

    def __init__(self, llm: Optional[LLMClient], registry: ToolRegistry, config=None):
        self.llm = llm
        self.registry = registry
        from configs.config_loader import get_config
        cfg = config or get_config()
        self._cfg = cfg
        det = cfg.detection
        self.sources_enabled = det.sources_enabled
        self.metric_checks = det.metric_checks or self._default_metric_checks()
        self.critical_event_reasons = set(det.critical_event_reasons)
        self.critical_pod_reasons = set(det.critical_pod_reasons)
        self._detection_cfg = {
            "default_detect_methods": getattr(det, "default_detect_methods", ["threshold", "zscore"]),
            "default_lookback_m": getattr(det, "default_lookback_m", 30),
            "default_z_threshold": getattr(det, "default_z_threshold", 3.0),
            "default_ewma_span": getattr(det, "default_ewma_span", 10),
            "categories_enabled": getattr(det, "categories_enabled", {}),
            "business_services": getattr(det, "business_services", []),
            "db_services": getattr(det, "db_services", []),
            "thresholds": getattr(det, "thresholds", {}),
            "namespace": getattr(cfg.kubernetes, "namespace", ""),
        }

    def detect(self, namespace: str = "") -> List[DetectionSignal]:
        """Run one detection cycle across all signal sources."""
        signals = []

        if self.sources_enabled.get("prometheus"):
            signals.extend(self._check_prometheus_alerts())

        if self.sources_enabled.get("k8s_event"):
            signals.extend(self._check_k8s_events(namespace))

        if self.sources_enabled.get("pod_health"):
            signals.extend(self._check_pod_health(namespace))

        if self.sources_enabled.get("node_health"):
            signals.extend(self._check_node_health())

        if self.sources_enabled.get("metric_anomaly"):
            signals.extend(self._check_metric_anomalies(namespace))

        logger.info(f"Detection cycle: found {len(signals)} signals")
        return signals

    def _check_prometheus_alerts(self) -> List[DetectionSignal]:
        """Surface anomalies inferred from MCP-backed golden metrics.

        The MCP workspace doesn't expose a Prometheus ALERTS series, so
        instead we ask the MCP `prometheus` tool for current pod metrics
        and emit a warning signal for any pod whose
        ``pod_cpu_usage_rate_vs_limit`` or
        ``pod_memory_usage_vs_limit`` is above 80%. This gives the alert
        centre actionable signals using live MCP data.
        """
        signals = []
        prom = self.registry.get("prometheus")
        if prom is None:
            return signals

        # per_entity_limit dropped to 8 to keep the alert centre snappy
        result = prom.execute(
            query="",
            per_entity=True,
            per_entity_limit=8,
            domain="k8s",
            entity_set_name="k8s.pod",
        )
        if not result.success:
            return signals

        for item in (result.data or {}).get("results", []):
            metric = item.get("metric", {})
            metric_name = metric.get("__name__", "")
            pod = metric.get("pod", "") or metric.get("service", "")
            values = item.get("values") or []
            if not values:
                continue
            try:
                latest_val = float(values[-1][1])
            except (ValueError, TypeError, IndexError):
                continue

            severity = None
            if metric_name in ("pod_cpu_usage_rate_vs_limit",
                                "pod_memory_usage_vs_limit"):
                # Thresholds are deliberately low so the alert centre
                # surfaces pods that are using a non-trivial slice of
                # their configured limit. Tune via config.
                if latest_val >= 50:
                    severity = "critical"
                elif latest_val >= 20:
                    severity = "warning"

            if severity is None:
                continue

            signals.append(DetectionSignal(
                signal_id="",
                source="prometheus",
                severity=severity,
                title=f"{metric_name} {latest_val:.1f}% on {pod}",
                description=(
                    f"Pod {pod} {metric_name} reached {latest_val:.1f}% "
                    f"of its configured limit (MCP golden metric)."
                ),
                namespace="",
                service=pod,
                raw_data=item,
            ))

        # APM service-level signals
        try:
            apm_result = prom.execute(query="", domain="apm", entity_set_name="apm.service")
            if apm_result.success:
                for item in (apm_result.data or {}).get("results", []):
                    metric = item.get("metric", {})
                    metric_name = metric.get("__name__", "")
                    values = item.get("values") or []
                    if not values:
                        continue
                    try:
                        latest_val = float(values[-1][1])
                    except (ValueError, TypeError, IndexError):
                        continue
                    severity = None
                    title = None
                    desc = None
                    if metric_name == "error_count" and latest_val > 0:
                        severity = "critical" if latest_val >= 50 else "warning"
                        title = f"APM error_count={int(latest_val)}"
                        desc = f"APM aggregated error_count is {int(latest_val)} (MCP golden metric)."
                    elif metric_name == "slow_count" and latest_val > 0:
                        severity = "critical" if latest_val >= 100 else "warning"
                        title = f"APM slow_count={int(latest_val)}"
                        desc = f"APM aggregated slow_count is {int(latest_val)} (MCP golden metric)."
                    elif metric_name == "avg_request_latency_seconds" and latest_val >= 0.5:
                        severity = "critical" if latest_val >= 2.0 else "warning"
                        title = f"APM latency {latest_val:.3f}s"
                        desc = f"APM avg request latency is {latest_val:.3f}s (MCP golden metric)."
                    if severity is None:
                        continue
                    signals.append(DetectionSignal(
                        signal_id="",
                        source="apm",
                        severity=severity,
                        title=title,
                        description=desc,
                        namespace="",
                        service="apm.service",
                        raw_data=item,
                    ))
        except Exception as exc:
            logger.debug("apm alert check failed: %s", exc)

        # ── K8s pod / node state signals (from MCP entities) ──
        try:
            from web_app.app import _mcp_browse_entities
        except Exception:
            _mcp_browse_entities = None

        if _mcp_browse_entities is not None:
            # Failed / Pending pods → one alert each (cap at 30 to avoid noise)
            try:
                pods = _mcp_browse_entities("k8s", "k8s.pod", 500)
                bad_pods = 0
                for p in pods:
                    if bad_pods >= 30:
                        break
                    phase = p.get("status") or ""
                    if phase in ("Failed", "Pending", "Unknown"):
                        signals.append(DetectionSignal(
                            signal_id="",
                            source="k8s_pod",
                            severity="critical" if phase == "Failed" else "warning",
                            title=f"Pod {phase}: {p.get('name','')}",
                            description=f"Pod {p.get('name','')} in namespace {p.get('namespace','')} is {phase}.",
                            namespace=p.get("namespace", ""),
                            service=p.get("name", ""),
                            raw_data=p,
                        ))
                        bad_pods += 1
                    # High restart count
                    rc = int(p.get("restart_count", 0) or 0)
                    if rc >= 5 and bad_pods < 30:
                        signals.append(DetectionSignal(
                            signal_id="",
                            source="k8s_pod",
                            severity="warning" if rc < 20 else "critical",
                            title=f"Pod restart_count={rc}: {p.get('name','')}",
                            description=f"Pod {p.get('name','')} has restarted {rc} times.",
                            namespace=p.get("namespace", ""),
                            service=p.get("name", ""),
                            raw_data=p,
                        ))
                        bad_pods += 1
            except Exception as exc:
                logger.debug("pod alert check failed: %s", exc)

            # Node conditions: parse the JSON-array status field and alert
            # only when Ready != True or any *Pressure / *Offline = True.
            try:
                import json as _json
                nodes = _mcp_browse_entities("k8s", "k8s.node", 100)
                for n in nodes:
                    raw_status = n.get("status") or ""
                    if not isinstance(raw_status, str) or not raw_status.startswith("["):
                        continue
                    try:
                        conds = _json.loads(raw_status)
                    except (ValueError, TypeError):
                        continue
                    bad_reasons = []
                    ready = True
                    for c in conds:
                        if not isinstance(c, dict):
                            continue
                        t = c.get("type", "")
                        st = c.get("status", "")
                        if t == "Ready" and st != "True":
                            ready = False
                        # NodeProblemDetector / pressure conditions = trouble when True
                        if st == "True" and t in (
                            "MemoryPressure", "DiskPressure", "PIDPressure",
                            "KernelDeadlock", "NTPProblem", "SystemdOffline",
                            "RuntimeOffline", "DockerOffline", "ReadonlyFilesystem",
                            "InodesPressure", "NodePIDPressure",
                        ):
                            bad_reasons.append(t)
                    if not ready:
                        signals.append(DetectionSignal(
                            signal_id="",
                            source="k8s_node",
                            severity="critical",
                            title=f"Node NotReady: {n.get('name','')}",
                            description=f"Node {n.get('name','')} Ready condition is False.",
                            namespace="",
                            service=n.get("name", ""),
                            raw_data=n,
                        ))
                    if bad_reasons:
                        signals.append(DetectionSignal(
                            signal_id="",
                            source="k8s_node",
                            severity="warning",
                            title=f"Node {n.get('name','')}: " + ", ".join(bad_reasons),
                            description=f"Node {n.get('name','')} conditions raised: {bad_reasons}",
                            namespace="",
                            service=n.get("name", ""),
                            raw_data=n,
                        ))
            except Exception as exc:
                logger.debug("node alert check failed: %s", exc)

        # ── Real CMS alert events from SLS (rca-benchmark workspace) ──
        try:
            cms_alerts = _fetch_cms_alerts_via_mcp(limit=1000, lookback="now-6h")
            # Dedupe by id so the same alert isn't surfaced N times.
            seen_ids = set()
            for a in cms_alerts:
                aid = a.get("id")
                if aid and aid in seen_ids:
                    continue
                if aid:
                    seen_ids.add(aid)
                sev = a.get("severity", "warning")
                if sev not in ("critical", "warning", "info"):
                    sev = "warning" if sev else "info"
                title = a.get("subject") or a.get("subtype") or "CMS alert"
                # Resource is JSON-encoded — try to extract instance_name / namespace.
                ns = ""
                obj = ""
                res_raw = a.get("resource", "")
                if isinstance(res_raw, str) and res_raw.startswith("{"):
                    try:
                        import json as _j
                        rj = _j.loads(res_raw)
                        tags = rj.get("tags", {}) if isinstance(rj, dict) else {}
                        ns = tags.get("namespace", "") or tags.get("k8s_namespace", "")
                        obj = (tags.get("instance_name", "")
                               or tags.get("pod_name", "")
                               or tags.get("service", ""))
                    except Exception:
                        pass
                description = title
                if a.get("annotations"):
                    text = " | ".join(
                        f"{k}={v}" for k, v in a["annotations"].items() if isinstance(v, str)
                    )
                    if text:
                        description = text[:500]
                signals.append(DetectionSignal(
                    signal_id="",
                    source="cms_alert",
                    severity=sev,
                    title=title,
                    description=description,
                    namespace=ns,
                    service=obj,
                    raw_data=a,
                ))
        except Exception as exc:
            logger.debug("cms alert pull failed: %s", exc)

        return signals

    def _check_k8s_events(self, namespace: str = "") -> List[DetectionSignal]:
        """Check for K8s Warning events."""
        signals = []
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return signals
        
        cmd = "get events --field-selector type=Warning --sort-by='.lastTimestamp' -o json"
        if namespace:
            cmd += f" -n {namespace}"
        else:
            cmd += " --all-namespaces"
        
        result = kubectl.execute(command=cmd, timeout=15)
        if not result.success:
            return signals
        
        import json
        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
            for event in (data or {}).get("items", [])[:20]:
                obj = event.get("involvedObject", {})
                msg = event.get("message", "")
                reason = event.get("reason", "")
                
                # Determine severity
                severity = "warning"
                if reason in self.critical_event_reasons:
                    severity = "critical"
                
                signals.append(DetectionSignal(
                    signal_id="",
                    source="k8s_event",
                    severity=severity,
                    title=f"{reason}: {obj.get('kind', '')}/{obj.get('name', '')}",
                    description=msg[:500],
                    namespace=obj.get("namespace", event.get("metadata", {}).get("namespace", "")),
                    service=obj.get("name", ""),
                    raw_data=event,
                ))
        except (json.JSONDecodeError, TypeError):
            pass
        
        return signals

    def _check_pod_health(self, namespace: str = "") -> List[DetectionSignal]:
        """Check for unhealthy pods."""
        signals = []
        k8s_health = self.registry.get("k8s_health")
        if k8s_health is None:
            return signals
        
        result = k8s_health.execute(component="pods")
        if not result.success:
            return signals
        
        for pod in (result.data or {}).get("checks", {}).get("pods", {}).get("problem_pods", []):
            reason = pod.get("reason", pod.get("phase", "Unknown"))
            severity = "critical" if reason in self.critical_pod_reasons else "warning"
            
            signals.append(DetectionSignal(
                signal_id="",
                source="pod_health",
                severity=severity,
                title=f"Unhealthy Pod: {pod.get('name', '')} ({reason})",
                description=f"Pod {pod['name']} in namespace {pod.get('namespace', '')} is {reason}. "
                           f"Restarts: {pod.get('restart_count', 'N/A')}",
                namespace=pod.get("namespace", ""),
                service=pod.get("name", ""),
            ))
        
        return signals

    # ── Metric anomaly thresholds ──────────────────────────────────────────
    @staticmethod
    def _default_metric_checks():
        """Fallback metric checks when none provided in config."""
        return [
            {
                "name": "node_cpu_usage",
                "query": 'avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance) * 100',
                "unit": "%", "label_key": "instance", "level": "node",
                "warn": 85, "crit": 95,
            },
            {
                "name": "node_memory_usage",
                "query": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
                "unit": "%", "label_key": "instance", "level": "node",
                "warn": 85, "crit": 95,
            },
            {
                "name": "node_disk_usage",
                "query": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
                "unit": "%", "label_key": "instance", "level": "node",
                "warn": 85, "crit": 95,
            },
            {
                "name": "container_cpu",
                "query": 'sum(rate(container_cpu_usage_seconds_total{container!=""}[5m])) by (pod, namespace) '
                         '/ sum(kube_pod_container_resource_limits{resource="cpu"}) by (pod, namespace) * 100',
                "unit": "%", "label_key": "pod", "ns_key": "namespace", "level": "container",
                "warn": 80, "crit": 95,
            },
            {
                "name": "container_memory",
                "query": 'sum(container_memory_working_set_bytes{container!=""}) by (pod, namespace) '
                         '/ sum(kube_pod_container_resource_limits{resource="memory"}) by (pod, namespace) * 100',
                "unit": "%", "label_key": "pod", "ns_key": "namespace", "level": "container",
                "warn": 80, "crit": 95,
            },
        ]

    def _check_metric_anomalies(self, namespace: str = "") -> List[DetectionSignal]:
        """Check key resource metrics via multi-algorithm detection engine."""
        prom = self.registry.get("prometheus")
        if prom is None:
            return []

        from agents.metric_anomaly_detector import MetricAnomalyDetector
        detector = MetricAnomalyDetector(
            prom_tool=prom,
            metric_checks=self.metric_checks,
            detection_cfg=self._detection_cfg,
        )
        return detector.detect(namespace)

    def _check_node_health(self) -> List[DetectionSignal]:
        """Check for unhealthy nodes."""
        signals = []
        k8s_health = self.registry.get("k8s_health")
        if k8s_health is None:
            return signals
        
        result = k8s_health.execute(component="nodes")
        if not result.success:
            return signals
        
        nodes = (result.data or {}).get("checks", {}).get("nodes", {})
        for node in nodes.get("nodes", []):
            if node.get("ready") != "True":
                signals.append(DetectionSignal(
                    signal_id="",
                    source="node_health",
                    severity="critical",
                    title=f"Node NotReady: {node.get('name', '')}",
                    description=f"Node {node['name']} is not ready. Conditions: {node.get('conditions', {})}",
                    service=node.get("name", ""),
                ))
        
        return signals
