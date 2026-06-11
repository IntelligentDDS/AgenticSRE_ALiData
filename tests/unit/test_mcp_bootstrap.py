from unittest.mock import MagicMock, patch
from tools.base_tool import ToolRegistry


def test_bootstrap_registers_three_tools(monkeypatch):
    fake_client = MagicMock()
    fake_client.list_tools.return_value = ["introduction", "umodel_get_logs"]

    with patch("tools.mcp_bootstrap.MCPClient", return_value=fake_client):
        from tools.mcp_bootstrap import init

        registry = ToolRegistry()
        registry.reset()

        config = {
            "observability": {
                "backend": "mcp",
                "mcp_endpoint": "http://localhost:7980/mcp",
                "mcp_timeout_seconds": 60,
                "mcp_transport_retry": 3,
                "default_region": "cn-hongkong",
                "default_domain": "apm",
                "default_entity_set": "apm.service",
                "default_log_set_domain": "apm",
                "default_log_set_name": "apm.custom.k8s-log-cfbbc0eabc19d43c0a6fb6889b4451ad0.recommendation-log.log",
                "default_trace_set_domain": "apm",
                "default_trace_set_name": "apm.trace.common",
            }
        }
        client = init(config)

        assert client is fake_client
        assert "prometheus" in registry
        assert "elasticsearch" in registry
        assert "jaeger" in registry
        registry.reset()


def test_bootstrap_rejects_non_mcp_backend():
    from tools.mcp_bootstrap import init
    import pytest
    config = {"observability": {"backend": "alidata"}}
    with pytest.raises(ValueError):
        init(config)
