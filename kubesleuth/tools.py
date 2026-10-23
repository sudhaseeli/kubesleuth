"""Tool definitions the agent can call, and the dispatcher that runs them.

The same definitions are sent to Claude (Anthropic tool format) and exposed by
the MCP server, so there is one source of truth for what the agent can do.
Investigation tools are read-only. The only tool that can change anything is
``propose_remediation``, and the agent loop routes it through a human approval
gate instead of dispatching it here.
"""

from __future__ import annotations

import json
from typing import Any

from .cluster.base import ClusterBackend, NotFound
from .models import ALLOWED_ACTIONS, CATEGORIES

METRICS = (
    "memory_working_set",
    "memory_limit",
    "cpu_usage",
    "restarts",
    "http_5xx_rate",
    "http_p99_latency",
    "node_disk_usage",
)

_ns = {"type": "string", "description": "Kubernetes namespace."}

READ_ONLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_pods",
        "description": (
            "List pods in a namespace with phase, ready count, restart count, node and the "
            "current waiting/terminated reason. Optionally filter by owning workload, "
            "e.g. 'deployment/checkout-api'. Start most investigations here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": _ns,
                "owner": {"type": "string", "description": "Optional owner filter, e.g. 'deployment/checkout-api'."},
            },
            "required": ["namespace"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_pod",
        "description": (
            "Full status of one pod: container states and last termination (reason, exit code), "
            "resource requests/limits, probes, conditions and node."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"namespace": _ns, "name": {"type": "string", "description": "Pod name."}},
            "required": ["namespace", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_pod_logs",
        "description": (
            "Read the tail of a container's logs. Set previous=true to read the logs of the "
            "last crashed instance, which is usually where the real error is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": _ns,
                "name": {"type": "string", "description": "Pod name."},
                "previous": {"type": "boolean", "description": "Read the previous (crashed) container instance."},
                "tail": {"type": "integer", "description": "Number of lines, default 50, max 200."},
            },
            "required": ["namespace", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_events",
        "description": "Recent Kubernetes events in a namespace, optionally only those about one object name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": _ns,
                "object": {"type": "string", "description": "Optional object name to filter on (pod, deployment or node)."},
            },
            "required": ["namespace"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_deployment",
        "description": (
            "A deployment's desired vs ready replicas, current image and its rollout history "
            "(revision, image, change cause, when it was rolled out). Use it to correlate an "
            "incident with a recent change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"namespace": _ns, "name": {"type": "string", "description": "Deployment name."}},
            "required": ["namespace", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_metric",
        "description": (
            "Fetch a Prometheus time series for a workload or node over the last window. "
            f"Supported metrics: {', '.join(METRICS)}. Returns timestamped values and a short summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": list(METRICS)},
                "namespace": _ns,
                "target": {"type": "string", "description": "Deployment name, or node name for node_disk_usage."},
                "window": {"type": "string", "description": "Lookback window like '30m' or '2h'. Default '1h'."},
            },
            "required": ["metric", "namespace", "target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_node_status",
        "description": "Node conditions (Ready, MemoryPressure, DiskPressure, PIDPressure), allocatable resources and taints. Omit name for all nodes.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Optional node name."}},
            "required": [],
            "additionalProperties": False,
        },
    },
]

PROPOSE_TOOL: dict[str, Any] = {
    "name": "propose_remediation",
    "description": (
        "Propose exactly one remediation once you are confident about the root cause. "
        "Nothing is executed until a human approves it; the result tells you whether it was "
        "approved and, if so, what happened after it was applied. Prefer the smallest safe change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ALLOWED_ACTIONS)},
            "namespace": _ns,
            "target": {"type": "string", "description": "Deployment name, or node name for cordon_node."},
            "params": {
                "type": "object",
                "description": (
                    "Action parameters: rollback_deployment {to_revision:int}; "
                    "patch_memory_limit {container:str, memory:'512Mi'}; scale_deployment {replicas:int}; "
                    "restart_deployment {}; cordon_node {}."
                ),
            },
            "rationale": {"type": "string", "description": "One or two sentences tying the action to the evidence."},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["action", "namespace", "target", "params", "rationale", "risk"],
        "additionalProperties": False,
    },
}

REPORT_TOOL: dict[str, Any] = {
    "name": "submit_report",
    "description": "Finish the investigation with a structured incident report. Call this exactly once, last.",
    "input_schema": {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string", "description": "Plain-language root cause an on-call engineer can act on."},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "confidence": {"type": "number", "description": "0 to 1."},
            "evidence": {"type": "array", "items": {"type": "string"}, "description": "Concrete observations that support the root cause."},
            "remediation": {"type": "string", "description": "What was done or what should be done."},
            "follow_ups": {"type": "array", "items": {"type": "string"}, "description": "Longer-term fixes to prevent a repeat."},
        },
        "required": ["root_cause", "category", "confidence", "evidence", "remediation", "follow_ups"],
        "additionalProperties": False,
    },
}

ALL_TOOLS = READ_ONLY_TOOLS + [PROPOSE_TOOL, REPORT_TOOL]
READ_ONLY_NAMES = {t["name"] for t in READ_ONLY_TOOLS}


class ToolError(Exception):
    pass


def _require(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"'{key}' is required")
    return value.strip()


class ToolRegistry:
    """Runs read-only tools against a cluster backend and returns JSON strings."""

    def __init__(self, backend: ClusterBackend):
        self.backend = backend

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Return (content, is_error). Never raises for bad input."""
        if name not in READ_ONLY_NAMES:
            return f"Error: '{name}' is not a read-only investigation tool.", True
        try:
            result = self._dispatch(name, args or {})
        except ToolError as e:
            return f"Error: {e}", True
        except NotFound as e:
            return f"Not found: {e}", True
        return json.dumps(result, indent=1, sort_keys=True, default=str), False

    def _dispatch(self, name: str, a: dict[str, Any]) -> Any:
        b = self.backend
        if name == "list_pods":
            return b.list_pods(_require(a, "namespace"), a.get("owner") or None)
        if name == "describe_pod":
            return b.describe_pod(_require(a, "namespace"), _require(a, "name"))
        if name == "get_pod_logs":
            tail = a.get("tail", 50)
            if not isinstance(tail, int) or tail < 1:
                tail = 50
            return b.pod_logs(_require(a, "namespace"), _require(a, "name"), bool(a.get("previous", False)), min(tail, 200))
        if name == "get_events":
            return b.events(_require(a, "namespace"), a.get("object") or None)
        if name == "get_deployment":
            return b.deployment(_require(a, "namespace"), _require(a, "name"))
        if name == "query_metric":
            metric = _require(a, "metric")
            if metric not in METRICS:
                raise ToolError(f"unknown metric '{metric}'; use one of {', '.join(METRICS)}")
            return b.metric(metric, _require(a, "namespace"), _require(a, "target"), str(a.get("window") or "1h"))
        if name == "get_node_status":
            return b.nodes(a.get("name") or None)
        raise ToolError(f"unknown tool '{name}'")
