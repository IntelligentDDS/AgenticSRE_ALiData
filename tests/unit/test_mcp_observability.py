"""Unit tests for MCP-backed SRETool adapters.

Strategy: replay-mode — mock MCPClient.call_tool to return fixtures
recorded from the live server. Verifies the adapter reshapes responses
into the legacy Prometheus / Elasticsearch / Jaeger format that agents
expect.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from tools.mcp_observability import MCPMetricTool, MCPLogTool, MCPTraceTool
from tools.mcp_exceptions import EntityNotFound, AuthError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mcp_replay"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _mock_client(tool_responses):
    """tool_responses: dict {mcp_tool_name: response_dict}"""
    client = MagicMock()
    def _call(tool_name, args):
        if tool_name not in tool_responses:
            raise KeyError(f"unexpected tool: {tool_name}")
        return tool_responses[tool_name]
    client.call_tool.side_effect = _call
    return client


def test_metric_tool_returns_prometheus_shape():
    fixture = _load("umodel_get_golden_metrics_sample.json")
    client = _mock_client({"umodel_get_golden_metrics": fixture})

    tool = MCPMetricTool(
        client=client,
        default_region="cn-hongkong",
        default_workspace="rca-benchmark",
        default_domain="k8s",
        default_entity_set="k8s.pod",
    )
    result = tool.execute(
        query="", entity_id="5923d7596dae6413e902be416bee5710",
        domain="k8s", entity_set_name="k8s.pod",
    )

    assert result.success
    assert result.source == "prometheus"
    data = result.data
    assert "results" in data and "result_count" in data
    assert isinstance(data["results"], list)
    # Live fixture has 6 metric series
    assert data["result_count"] == 6
    sample = data["results"][0]
    assert "metric" in sample and "values" in sample
    assert "__name__" in sample["metric"]
    # Values should be [ts_seconds, "value_str"] pairs
    ts, val = sample["values"][0]
    assert isinstance(ts, int)
    # Recorded fixture: ts is nanos; adapter must convert to seconds
    assert 1e9 < ts < 1e11  # seconds-range (2001 .. ~5138)
    assert isinstance(val, str)


def test_metric_tool_query_filter_matches_substring():
    fixture = _load("umodel_get_golden_metrics_sample.json")
    client = _mock_client({"umodel_get_golden_metrics": fixture})
    tool = MCPMetricTool(
        client=client, default_region="cn-hongkong",
        default_workspace="rca-benchmark",
        default_domain="k8s", default_entity_set="k8s.pod",
    )
    result = tool.execute(query="cpu", entity_id="x", domain="k8s", entity_set_name="k8s.pod")
    # Live fixture has 2 cpu metrics out of 6
    assert result.data["result_count"] == 2
    assert "cpu" in result.data["results"][0]["metric"]["__name__"]


def test_log_tool_returns_es_shape():
    fixture = _load("umodel_get_logs_sample.json")
    client = _mock_client({"umodel_get_logs": fixture})

    tool = MCPLogTool(
        client=client, default_region="cn-hongkong",
        default_workspace="rca-benchmark",
        default_domain="apm", default_entity_set="apm.service",
        default_log_set_domain="apm",
        default_log_set_name="apm.custom.k8s-log-cfbbc0eabc19d43c0a6fb6889b4451ad0.recommendation-log.log",
    )
    result = tool.execute(query="", time_range="1h", size=20)

    assert result.success
    assert result.source == "elasticsearch"
    data = result.data
    assert "total_hits" in data
    assert "returned" in data
    assert isinstance(data["entries"], list)
    assert data["returned"] == 20  # capped by size
    e = data["entries"][0]
    for key in ("timestamp", "level", "message", "pod", "namespace"):
        assert key in e


def test_log_tool_filters_by_query_substring():
    fixture = _load("umodel_get_logs_sample.json")
    client = _mock_client({"umodel_get_logs": fixture})
    tool = MCPLogTool(
        client=client, default_region="cn-hongkong",
        default_workspace="rca-benchmark",
        default_domain="apm", default_entity_set="apm.service",
        default_log_set_domain="apm",
        default_log_set_name="apm.custom.k8s-log-cfbbc0eabc19d43c0a6fb6889b4451ad0.recommendation-log.log",
    )
    # 'OLJCESPC7Z' appears in the sample we saw
    result = tool.execute(query="OLJCESPC7Z", size=200)
    assert result.success
    assert result.data["returned"] >= 1
    for entry in result.data["entries"]:
        assert "OLJCESPC7Z" in entry["message"]


def test_trace_tool_returns_jaeger_shape():
    search_fx = _load("umodel_search_traces_sample.json")
    detail_fx = _load("umodel_get_traces_sample.json")
    client = _mock_client({
        "umodel_search_traces": search_fx,
        "umodel_get_traces": detail_fx,
    })

    tool = MCPTraceTool(
        client=client, default_region="cn-hongkong",
        default_workspace="rca-benchmark",
        default_domain="apm", default_entity_set="apm.service",
        default_trace_set_domain="apm",
        default_trace_set_name="apm.trace.common",
    )
    result = tool.execute(service="review", lookback="1h", limit=5)

    assert result.success
    assert result.source == "jaeger"
    data = result.data
    assert "service" in data
    assert "trace_count" in data
    assert isinstance(data["traces"], list)
    # search fixture has 5 trace summaries
    assert data["trace_count"] == 5
    t = data["traces"][0]
    assert "trace_id" in t
    assert "duration_ms" in t


def test_trace_tool_exact_lookup_groups_spans():
    detail_fx = _load("umodel_get_traces_sample.json")
    client = _mock_client({"umodel_get_traces": detail_fx})
    tool = MCPTraceTool(
        client=client, default_region="cn-hongkong",
        default_workspace="rca-benchmark",
        default_domain="apm", default_entity_set="apm.service",
        default_trace_set_domain="apm",
        default_trace_set_name="apm.trace.common",
    )
    result = tool.execute(trace_id="b96a6018d5d3c78d95449347b6a6cd05", lookback="1h")
    assert result.success
    traces = result.data["traces"]
    # Exact lookup returns 1 trace assembled from N spans
    assert len(traces) == 1
    t = traces[0]
    assert t["trace_id"] == "b96a6018d5d3c78d95449347b6a6cd05"
    assert "spans" in t
    assert len(t["spans"]) == 4  # detail fixture has 4 spans
    assert "services" in t


def test_metric_tool_promotes_entity_not_found():
    from tools.mcp_exceptions import MCPToolError
    def _raise(_name, _args):
        raise MCPToolError(
            message="entity not found",
            tool_name="umodel_get_golden_metrics",
            code="EntityNotFound", raw={},
        )
    client = MagicMock()
    client.call_tool.side_effect = _raise

    tool = MCPMetricTool(client=client)
    with pytest.raises(EntityNotFound):
        tool._execute(entity_id="missing", domain="k8s", entity_set_name="k8s.pod")


def test_log_tool_promotes_auth_error():
    from tools.mcp_exceptions import MCPToolError
    def _raise(_name, _args):
        raise MCPToolError(
            message="401 unauthorized",
            tool_name="umodel_get_logs",
            code="Unauthorized", raw={},
        )
    client = MagicMock()
    client.call_tool.side_effect = _raise

    tool = MCPLogTool(client=client)
    with pytest.raises(AuthError):
        tool._execute()
