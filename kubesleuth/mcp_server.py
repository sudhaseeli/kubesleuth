"""Expose KubeSleuth's read-only investigation tools as an MCP server.

Any MCP client (Claude Desktop, Claude Code, your own agent) can then
investigate a cluster with the same tools KubeSleuth uses. Remediation is
deliberately not exposed here: changes only happen through the agent's
approval gate.

    kubesleuth mcp --scenario oom-kill          # simulated cluster
    kubesleuth mcp --live --namespace shop      # current kubeconfig context
"""

from __future__ import annotations

from typing import Any

from .cluster.base import ClusterBackend
from .tools import ToolRegistry


def build_server(backend: ClusterBackend) -> Any:
    try:
        from mcp.server.mcpserver import MCPServer as Server  # MCP SDK 2.x
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # MCP SDK 1.x
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise RuntimeError("The MCP server needs the MCP SDK: pip install 'kubesleuth[mcp]'") from e

    registry = ToolRegistry(backend)
    server = Server("kubesleuth")

    def call(_tool: str, /, **args: Any) -> str:
        content, is_error = registry.run(_tool, {k: v for k, v in args.items() if v is not None})
        if is_error:
            raise ValueError(content)
        return content

    @server.tool()
    def current_alert() -> str:
        """The alert that triggered this investigation."""
        import json
        return json.dumps(backend.alert(), indent=1)

    @server.tool()
    def list_pods(namespace: str, owner: str | None = None) -> str:
        """List pods with phase, readiness, restarts and current waiting/terminated reason. owner filters by workload, e.g. 'deployment/checkout-api'."""
        return call("list_pods", namespace=namespace, owner=owner)

    @server.tool()
    def describe_pod(namespace: str, name: str) -> str:
        """Container states, last termination reason and exit code, resources, probes and conditions for one pod."""
        return call("describe_pod", namespace=namespace, name=name)

    @server.tool()
    def get_pod_logs(namespace: str, name: str, previous: bool = False, tail: int = 50) -> str:
        """Tail of a container's logs. previous=true reads the last crashed instance."""
        return call("get_pod_logs", namespace=namespace, name=name, previous=previous, tail=tail)

    @server.tool()
    def get_events(namespace: str, object: str | None = None) -> str:
        """Recent Kubernetes events, optionally only those about one object name."""
        return call("get_events", namespace=namespace, object=object)

    @server.tool()
    def get_deployment(namespace: str, name: str) -> str:
        """Desired vs ready replicas, current image and rollout history of a deployment."""
        return call("get_deployment", namespace=namespace, name=name)

    @server.tool()
    def query_metric(metric: str, namespace: str, target: str, window: str = "1h") -> str:
        """Prometheus series: memory_working_set, memory_limit, cpu_usage, restarts, http_5xx_rate, http_p99_latency or node_disk_usage."""
        return call("query_metric", metric=metric, namespace=namespace, target=target, window=window)

    @server.tool()
    def get_node_status(name: str | None = None) -> str:
        """Node conditions (Ready, MemoryPressure, DiskPressure, PIDPressure), allocatable resources and taints."""
        return call("get_node_status", name=name)

    return server


def serve(backend: ClusterBackend) -> None:
    build_server(backend).run()
