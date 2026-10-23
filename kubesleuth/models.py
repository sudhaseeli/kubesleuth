"""Plain data types shared by the agent, the brains and the cluster backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Remediations the agent is allowed to propose. Anything else is rejected
# before it ever reaches a human, let alone the cluster.
ALLOWED_ACTIONS = (
    "rollback_deployment",
    "patch_memory_limit",
    "restart_deployment",
    "scale_deployment",
    "cordon_node",
)

# Namespaces the agent may never propose changes in.
PROTECTED_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")

CATEGORIES = (
    "OOMKilled",
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "BadRollout",
    "NodePressure",
    "ProbeFailure",
    "Unknown",
)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Step:
    """One decision from a brain: optional narration plus zero or more tool calls."""

    thought: str = ""
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Proposal:
    action: str
    namespace: str
    target: str
    params: dict[str, Any]
    rationale: str
    risk: str

    @classmethod
    def from_input(cls, data: dict[str, Any]) -> "Proposal":
        return cls(
            action=str(data.get("action", "")),
            namespace=str(data.get("namespace", "")),
            target=str(data.get("target", "")),
            params=dict(data.get("params") or {}),
            rationale=str(data.get("rationale", "")),
            risk=str(data.get("risk", "medium")),
        )

    def validate(self) -> str | None:
        """Return an error message, or None if the proposal is acceptable."""
        if self.action not in ALLOWED_ACTIONS:
            return f"action '{self.action}' is not allowed; choose one of {', '.join(ALLOWED_ACTIONS)}"
        if self.namespace in PROTECTED_NAMESPACES:
            return f"changes in namespace '{self.namespace}' are not allowed"
        if not self.target:
            return "target is required"
        if self.action == "patch_memory_limit" and not self.params.get("memory"):
            return "patch_memory_limit needs params.memory, e.g. '512Mi'"
        if self.action == "scale_deployment" and not isinstance(self.params.get("replicas"), int):
            return "scale_deployment needs an integer params.replicas"
        if self.action == "rollback_deployment" and "to_revision" in self.params:
            if not isinstance(self.params["to_revision"], int):
                return "params.to_revision must be an integer"
        return None

    def describe(self) -> str:
        extra = ", ".join(f"{k}={v}" for k, v in self.params.items())
        where = f"{self.namespace}/{self.target}" if self.namespace else self.target
        return f"{self.action} {where}" + (f" ({extra})" if extra else "")


@dataclass
class Report:
    root_cause: str
    category: str
    confidence: float
    evidence: list[str]
    remediation: str
    follow_ups: list[str]
    status: str = "diagnosed"  # diagnosed | resolved | awaiting_human

    @classmethod
    def from_input(cls, data: dict[str, Any]) -> "Report":
        category = str(data.get("category", "Unknown"))
        if category not in CATEGORIES:
            category = "Unknown"
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            root_cause=str(data.get("root_cause", "")),
            category=category,
            confidence=max(0.0, min(1.0, confidence)),
            evidence=[str(e) for e in data.get("evidence") or []],
            remediation=str(data.get("remediation", "")),
            follow_ups=[str(f) for f in data.get("follow_ups") or []],
        )

    def to_markdown(self) -> str:
        lines = [
            "## Incident report",
            "",
            f"**Status:** {self.status.replace('_', ' ')}  ",
            f"**Category:** {self.category}  ",
            f"**Confidence:** {round(self.confidence * 100)}%",
            "",
            "### Root cause",
            self.root_cause,
            "",
            "### Evidence",
            *[f"- {e}" for e in self.evidence],
            "",
            "### Remediation",
            self.remediation,
        ]
        if self.follow_ups:
            lines += ["", "### Follow-ups", *[f"- {f}" for f in self.follow_ups]]
        return "\n".join(lines)


@dataclass
class Event:
    """Something that happened during an investigation, for the CLI, traces and the demo."""

    kind: str  # thought | tool_call | tool_result | approval_request | approval | action_result | report | error
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "data": self.data}


def to_plain(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return obj
