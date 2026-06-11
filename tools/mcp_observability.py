"""MCP-backed SRETool adapters.

Three tools preserve the names ``prometheus`` / ``elasticsearch`` /
``jaeger`` so registered consumers (16 agents + web_app + eval) see no
change. Each adapter calls one or more ``umodel_*`` MCP tools via a
shared ``MCPClient`` and reshapes the response into the legacy format
the original Prometheus / ES / Jaeger / AliData backends produced.
"""
from __future__ import annotations

import json
import logging
import time as _time
from typing import Any, Dict, List, Optional

from .base_tool import SRETool, ToolResult
from .mcp_client import MCPClient
from .mcp_exceptions import (
    MCPError, MCPToolError,
    EntityNotFound, AuthError, QueryError,
)

logger = logging.getLogger(__name__)


_TIME_RANGE_SECONDS = {
    "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "3h": 10800, "6h": 21600,
    "12h": 43200, "24h": 86400, "7d": 604800,
}


def _parse_range_seconds(s: str) -> int:
    return _TIME_RANGE_SECONDS.get(s, 3600)


def _classify_tool_error(e: MCPToolError) -> MCPToolError:
    """Promote generic MCPToolError to specific subclasses based on code/text."""
    code = (e.code or "").lower()
    msg = str(e).lower()
    if "notfound" in code or "not found" in msg or "no such entity" in msg:
        return EntityNotFound(str(e), tool_name=e.tool_name, code=e.code, raw=e.raw)
    if "unauth" in code or "permission" in msg or "401" in msg or "403" in msg:
        return AuthError(str(e), tool_name=e.tool_name, code=e.code, raw=e.raw)
    if "syntax" in msg or "invalid query" in msg or "query" in code:
        return QueryError(str(e), tool_name=e.tool_name, code=e.code, raw=e.raw)
    return e


def _decode_str_array(raw: Any) -> List[Any]:
    """Decode the JSON-encoded string arrays the MCP server returns for __ts__/__value__."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return []
    return []


# ───────────── Metric Tool (Prometheus shape) ─────────────

class MCPMetricTool(SRETool):
    """Fetches metrics via ``umodel_get_golden_metrics`` and returns
    Prometheus-compatible envelope."""

    name = "prometheus"
    description = "Fetch metrics via MCP umodel_* tools — Prometheus-compatible adapter"

    def __init__(
        self,
        client: MCPClient,
        default_region: str = "cn-hongkong",
        default_workspace: str = "",
        default_domain: str = "k8s",
        default_entity_set: str = "k8s.pod",
    ):
        self.client = client
        self.default_region = default_region
        self.default_workspace = default_workspace
        self.default_domain = default_domain
        self.default_entity_set = default_entity_set

    def _execute(
        self,
        query: str = "",
        query_type: str = "instant",
        start: str = "",
        end: str = "",
        step: str = "60s",
        natural_language: str = "",
        namespace: str = "",
        entity_id: str = "",
        domain: str = "",
        entity_set_name: str = "",
        max_results: Optional[int] = 50,
        per_entity: bool = False,
        per_entity_limit: int = 10,
    ) -> ToolResult:
        effective_query = query or natural_language or ""

        if "ALERTS" in effective_query:
            return ToolResult(success=True, data={
                "query": effective_query, "result_count": 0, "results": [],
            })

        try:
            # Per-entity mode: list entities first, then issue one
            # umodel_get_golden_metrics call per entity so each pod's
            # data is a distinct series (label = pod name).
            if per_entity and not entity_id:
                return self._per_entity_sweep(
                    effective_query=effective_query,
                    namespace=namespace,
                    start=start, end=end,
                    domain=domain or self.default_domain,
                    entity_set_name=entity_set_name or self.default_entity_set,
                    per_entity_limit=per_entity_limit,
                    max_results=max_results,
                )

            # When the caller passes an explicit domain/entity_set, query just that
            # combination. Otherwise sweep the common (domain, entity_set) pairs and
            # merge so the dashboard sees a richer dataset.
            combos = []
            if domain or entity_set_name:
                combos.append((
                    domain or self.default_domain,
                    entity_set_name or self.default_entity_set,
                ))
            else:
                combos = [
                    ("k8s", "k8s.pod"),
                    ("k8s", "k8s.node"),
                    ("apm", "apm.service"),
                ]

            from_arg = self._to_time_arg(start, "now-1h")
            to_arg = self._to_time_arg(end, "now")

            results: List[Dict[str, Any]] = []
            for d, esn in combos:
                args: Dict[str, Any] = {
                    "regionId": self.default_region,
                    "workspace": self.default_workspace,
                    "domain": d,
                    "entity_set_name": esn,
                    "from_time": from_arg,
                    "to_time": to_arg,
                }
                if entity_id:
                    args["entity_ids"] = entity_id
                if len(combos) == 1:
                    # Single explicit combo — propagate semantic errors so the
                    # caller (or unit test) sees EntityNotFound / AuthError.
                    raw = self.client.call_tool("umodel_get_golden_metrics", args)
                else:
                    try:
                        raw = self.client.call_tool("umodel_get_golden_metrics", args)
                    except MCPToolError as e:
                        logger.warning("skip combo (%s, %s): %s", d, esn, e)
                        continue
                results.extend(
                    self._to_prom_format(raw, effective_query, namespace, max_results)
                )
                if max_results and len(results) >= max_results:
                    break

            return ToolResult(success=True, data={
                "query": effective_query,
                "result_count": len(results),
                "results": results[:max_results] if max_results else results,
            })

        except MCPToolError as e:
            raise _classify_tool_error(e)
        except MCPError as e:
            return ToolResult(success=False, error=f"MCP error: {e}")

    @staticmethod
    def _to_time_arg(v: str, default: str) -> Any:
        if not v:
            return default
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return v  # already in "now-1h" form

    @staticmethod
    def _to_prom_format(
        raw: Any,
        query: str,
        namespace: str,
        max_results: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Reshape umodel_get_golden_metrics into Prometheus result format.

        Source shape (verified against rca-benchmark workspace):
            {
              "data": [
                {
                  "metric": "pod_cpu_usage_rate",
                  "__ts__": "[1780719713000000000, ...]",   # JSON string of nanoseconds
                  "__value__": "[37691392.0, ...]",         # JSON string of floats
                  "__labels__": "{}",
                  "metric_set_id": "...",
                  "__name__": "null",
                  "__source__": "",
                },
                ...
              ]
            }
        """
        if not isinstance(raw, dict):
            return []
        items = raw.get("data") or raw.get("metrics") or []
        if not isinstance(items, list):
            return []

        results: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            metric_name = item.get("metric") or item.get("__name__") or item.get("metric_name") or ""
            if not metric_name or metric_name == "null":
                metric_name = item.get("metric_set_id", "")

            if query and query.lower() not in str(metric_name).lower():
                continue

            ts_arr = _decode_str_array(item.get("__ts__") or item.get("timestamps"))
            val_arr = _decode_str_array(item.get("__value__") or item.get("values"))

            values: List[List[Any]] = []
            for i, ts_ns in enumerate(ts_arr):
                if i >= len(val_arr):
                    break
                try:
                    ts_sec = int(float(ts_ns) / 1e9)
                except (TypeError, ValueError):
                    continue
                values.append([ts_sec, str(val_arr[i])])

            labels_raw = item.get("__labels__") or "{}"
            try:
                labels = json.loads(labels_raw) if isinstance(labels_raw, str) else (labels_raw or {})
            except (ValueError, TypeError):
                labels = {}

            metric_block = {
                "__name__": metric_name,
                "namespace": namespace,
            }
            if isinstance(labels, dict):
                metric_block.update(labels)

            results.append({
                "metric": metric_block,
                "values": values,
                "value": values[-1] if values else [0, "0"],
            })
            if max_results and len(results) >= max_results:
                break
        return results

    def _per_entity_sweep(
        self,
        *,
        effective_query: str,
        namespace: str,
        start: str, end: str,
        domain: str,
        entity_set_name: str,
        per_entity_limit: int,
        max_results: Optional[int],
    ) -> ToolResult:
        """One MCP call per entity so each pod/service is a distinct series.

        Side-effect: each result has the entity's readable name injected
        into the Prometheus-shape ``metric`` block as the ``pod`` /
        ``service`` label (matching the legacy Prometheus convention the
        dashboard expects).
        """
        from_arg = self._to_time_arg(start, "now-1h")
        to_arg = self._to_time_arg(end, "now")
        try:
            ents = self.client.call_tool("umodel_get_entities", {
                "regionId": self.default_region,
                "workspace": self.default_workspace,
                "domain": domain,
                "entity_set_name": entity_set_name,
                "limit": per_entity_limit,
                "from_time": from_arg,
                "to_time": to_arg,
            })
        except MCPToolError as e:
            return ToolResult(success=False, error=f"entity browse failed: {e}")

        entity_records = ents.get("data") or []
        results: List[Dict[str, Any]] = []

        # Per-entity label slot in Prometheus shape — pods use ``pod``,
        # services use ``service``.
        label_key = "service" if entity_set_name.endswith(".service") else "pod"

        for ent in entity_records[:per_entity_limit]:
            eid = ent.get("__entity_id__")
            if not eid:
                continue
            display_name = (
                ent.get("name") or ent.get("service")
                or ent.get("pod_name") or eid[:12]
            )
            try:
                raw = self.client.call_tool("umodel_get_golden_metrics", {
                    "regionId": self.default_region,
                    "workspace": self.default_workspace,
                    "domain": domain,
                    "entity_set_name": entity_set_name,
                    "entity_ids": eid,
                    "from_time": from_arg,
                    "to_time": to_arg,
                })
            except MCPToolError as e:
                logger.warning("skip entity %s: %s", display_name, e)
                continue
            for r in self._to_prom_format(raw, effective_query, namespace, max_results=None):
                # Inject identity into the metric labels so the dashboard
                # can group / colour by entity.
                r["metric"][label_key] = display_name
                r["metric"]["entity_id"] = eid
                results.append(r)
            if max_results and len(results) >= max_results:
                break

        return ToolResult(success=True, data={
            "query": effective_query,
            "result_count": len(results),
            "results": results[:max_results] if max_results else results,
        })

    def health_check(self) -> bool:
        try:
            self.client.call_tool("introduction", {})
            return True
        except Exception:
            return False


# ───────────── Log Tool (Elasticsearch shape) ─────────────


def _extract_level(content: str) -> str:
    """Heuristically pull a log level out of the log line."""
    up = content.upper()
    if "ERROR" in up or "EXCEPTION" in up or "TRACEBACK" in up:
        return "error"
    if "WARN" in up:
        return "warn"
    return "info"


class MCPLogTool(SRETool):
    """Fetches logs via ``umodel_get_logs``, returns ES-compatible envelope.

    Source shape (verified):
        {"data": [{"content": "...", "_time_": "...", "_pod_name_": "...",
                    "_namespace_": "...", "_container_name_": "...", ...}, ...]}
    """

    name = "elasticsearch"
    description = "Fetch logs via MCP umodel_get_logs — Elasticsearch-compatible adapter"

    def __init__(
        self,
        client: MCPClient,
        default_region: str = "cn-hongkong",
        default_workspace: str = "",
        default_domain: str = "apm",
        default_entity_set: str = "apm.service",
        default_log_set_domain: str = "apm",
        default_log_set_name: str = "",
    ):
        self.client = client
        self.default_region = default_region
        self.default_workspace = default_workspace
        self.default_domain = default_domain
        self.default_entity_set = default_entity_set
        self.default_log_set_domain = default_log_set_domain
        self.default_log_set_name = default_log_set_name

    def _execute(
        self,
        query: str = "",
        index: str = "",
        time_range: str = "1h",
        level: str = "",
        size: int = 100,
        namespace: str = "",
        entity_id: str = "",
        log_set_name: str = "",
        log_set_domain: str = "",
        domain: str = "",
        entity_set_name: str = "",
    ) -> ToolResult:
        try:
            span = _parse_range_seconds(time_range)
            args: Dict[str, Any] = {
                "regionId": self.default_region,
                "workspace": self.default_workspace,
                "domain": domain or self.default_domain,
                "entity_set_name": entity_set_name or self.default_entity_set,
                "log_set_domain": log_set_domain or self.default_log_set_domain,
                "log_set_name": log_set_name or self.default_log_set_name,
                "from_time": f"now-{span}s",
                "to_time": "now",
            }
            if entity_id:
                args["entity_ids"] = entity_id

            raw = self.client.call_tool("umodel_get_logs", args)
            entries = self._to_es_entries(raw, query, level, namespace, size)
            return ToolResult(success=True, data={
                "total_hits": len(entries),
                "returned": len(entries),
                "entries": entries,
            })
        except MCPToolError as e:
            raise _classify_tool_error(e)
        except MCPError as e:
            return ToolResult(success=False, error=f"MCP error: {e}")

    @staticmethod
    def _to_es_entries(
        raw: Any, query: str, level: str, namespace: str, size: int,
    ) -> List[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return []
        items = raw.get("data") or raw.get("logs") or raw.get("entries") or []
        if not isinstance(items, list):
            return []

        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            item_ns = item.get("_namespace_") or item.get("namespace") or ""
            if namespace and item_ns and namespace.lower() != item_ns.lower():
                continue

            content = item.get("content") or item.get("message") or ""
            norm_level = _extract_level(str(content))
            if level and level.lower() != norm_level:
                continue

            if query and query.lower() not in str(content).lower():
                continue

            out.append({
                "timestamp": item.get("_time_") or item.get("timestamp") or "",
                "level": norm_level,
                "message": str(content)[:500],
                "pod": item.get("_pod_name_") or item.get("pod") or "",
                "namespace": item_ns,
                "service": item.get("_container_name_") or item.get("service") or "",
                "container_ip": item.get("_container_ip_") or "",
            })
            if len(out) >= size:
                break
        return out

    def health_check(self) -> bool:
        try:
            self.client.call_tool("introduction", {})
            return True
        except Exception:
            return False


# ───────────── Trace Tool (Jaeger shape) ─────────────


def _to_int_safe(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


class MCPTraceTool(SRETool):
    """Fetches traces via ``umodel_search_traces`` (summary) +
    ``umodel_get_traces`` (spans). Returns Jaeger-compatible envelope.

    Source shapes (verified):
      umodel_search_traces ``data``: list of {"traceId", "duration_ms",
        "span_count", "error_span_count", ...}
      umodel_get_traces ``data``: list of span dicts with {"traceId",
        "span_id", "serviceName", "spanName", "duration_ms", "parentSpanId",
        "attributes", "kind", ...}
    """

    name = "jaeger"
    description = "Fetch traces via MCP umodel_* tools — Jaeger-compatible adapter"

    def __init__(
        self,
        client: MCPClient,
        default_region: str = "cn-hongkong",
        default_workspace: str = "",
        default_domain: str = "apm",
        default_entity_set: str = "apm.service",
        default_trace_set_domain: str = "apm",
        default_trace_set_name: str = "apm.trace.common",
    ):
        self.client = client
        self.default_region = default_region
        self.default_workspace = default_workspace
        self.default_domain = default_domain
        self.default_entity_set = default_entity_set
        self.default_trace_set_domain = default_trace_set_domain
        self.default_trace_set_name = default_trace_set_name

    def _execute(
        self,
        service: str = "",
        operation: str = "",
        min_duration: str = "",
        max_duration: str = "",
        limit: int = 20,
        lookback: str = "1h",
        trace_id: str = "",
        entity_id: str = "",
        domain: str = "",
        entity_set_name: str = "",
        trace_set_name: str = "",
        trace_set_domain: str = "",
    ) -> ToolResult:
        try:
            span = _parse_range_seconds(lookback)
            base_args: Dict[str, Any] = {
                "regionId": self.default_region,
                "workspace": self.default_workspace,
                "domain": domain or self.default_domain,
                "entity_set_name": entity_set_name or self.default_entity_set,
                "trace_set_domain": trace_set_domain or self.default_trace_set_domain,
                "trace_set_name": trace_set_name or self.default_trace_set_name,
                "from_time": f"now-{span}s",
                "to_time": "now",
            }

            # Exact lookup: fetch spans, group into one trace summary
            if trace_id:
                args = dict(base_args)
                args["trace_ids"] = trace_id
                detail = self.client.call_tool("umodel_get_traces", args)
                traces = self._spans_to_traces(self._extract_items(detail))
                return ToolResult(success=True, data=self._envelope(service, traces))

            # Browse: search summaries
            args = dict(base_args)
            args["limit"] = limit
            if entity_id:
                args["entity_ids"] = entity_id
            if min_duration:
                args["min_duration_ms"] = _to_int_safe(min_duration)
            if max_duration:
                args["max_duration_ms"] = _to_int_safe(max_duration)

            search = self.client.call_tool("umodel_search_traces", args)
            traces = self._summaries_to_traces(self._extract_items(search))
            return ToolResult(success=True, data=self._envelope(service, traces))
        except MCPToolError as e:
            raise _classify_tool_error(e)
        except MCPError as e:
            return ToolResult(success=False, error=f"MCP error: {e}")

    @staticmethod
    def _extract_items(raw: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return []
        data = raw.get("data") or raw.get("traces") or []
        return data if isinstance(data, list) else []

    @staticmethod
    def _summaries_to_traces(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            tid = it.get("traceId") or it.get("trace_id") or it.get("__trace_id__") or ""
            out.append({
                "trace_id": tid,
                "duration_ms": _to_int_safe(it.get("duration_ms")),
                "span_count": _to_int_safe(it.get("span_count")),
                "error_span_count": _to_int_safe(it.get("error_span_count")),
                "spans": [],
                "services": [],
            })
        return out

    @staticmethod
    def _spans_to_traces(spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Group span list by traceId, build one summary per trace."""
        from collections import defaultdict
        by_tid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for sp in spans:
            if not isinstance(sp, dict):
                continue
            tid = sp.get("traceId") or sp.get("__trace_id__") or sp.get("trace_id") or ""
            if tid:
                by_tid[tid].append(sp)

        out = []
        for tid, sp_list in by_tid.items():
            services = sorted({sp.get("serviceName") for sp in sp_list if sp.get("serviceName")})
            duration_ms = max(
                (_to_int_safe(sp.get("duration_ms")) for sp in sp_list),
                default=0,
            )
            out.append({
                "trace_id": tid,
                "duration_ms": duration_ms,
                "span_count": len(sp_list),
                "error_span_count": sum(
                    1 for sp in sp_list
                    if str(sp.get("statusCode", "")).upper() == "ERROR"
                ),
                "spans": sp_list,
                "services": list(services),
            })
        return out

    @staticmethod
    def _envelope(service: str, traces: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "service": service,
            "trace_count": len(traces),
            "traces": traces,
        }

    def health_check(self) -> bool:
        try:
            self.client.call_tool("introduction", {})
            return True
        except Exception:
            return False
