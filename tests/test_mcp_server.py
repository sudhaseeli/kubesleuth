import asyncio
import json

import pytest

pytest.importorskip("mcp")

from kubesleuth.cluster.simulated import SimulatedCluster  # noqa: E402
from kubesleuth.mcp_server import build_server  # noqa: E402


def test_mcp_server_exposes_only_read_only_tools():
    server = build_server(SimulatedCluster("oom-kill"))
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"list_pods", "describe_pod", "get_pod_logs", "get_events", "get_deployment", "query_metric", "get_node_status"} <= names
    assert "propose_remediation" not in names


def test_mcp_tool_call_returns_cluster_data():
    server = build_server(SimulatedCluster("oom-kill"))
    result = asyncio.run(server.call_tool("get_pod_logs", {"namespace": "shop", "name": "checkout-api-7d9f8b6c5-8rmvd", "previous": True, "tail": 2}))
    assert "RSS 252 MiB" in json.dumps(result, default=str)
