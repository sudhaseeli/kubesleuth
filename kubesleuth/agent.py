"""The investigation loop: brain decides, tools observe, a human approves changes."""

from __future__ import annotations

import json
from typing import Any, Callable

from .brains.base import Brain, BrainError
from .cluster.base import ClusterBackend
from .models import Event, Proposal, Report, ToolResult
from .tools import ALL_TOOLS, PROPOSE_TOOL, REPORT_TOOL, ToolRegistry

Approver = Callable[[Proposal], bool]
Listener = Callable[[Event], None]

NUDGE = (
    "You have not submitted a report yet. If you need more evidence, call more tools; "
    "otherwise call submit_report now."
)


def build_task(alert: dict[str, Any]) -> str:
    return (
        "An alert just fired. Investigate it, find the root cause, propose one safe fix if you are "
        "confident, and finish with submit_report.\n\n"
        f"Alert: {alert['name']} ({alert.get('severity', 'unknown')} severity)\n"
        f"Namespace: {alert['namespace']}\n"
        f"Target: {alert['target']}\n"
        f"Fired at: {alert.get('fired_at', 'unknown')}\n"
        f"Summary: {alert['summary']}"
    )


def deny_all(_: Proposal) -> bool:
    return False


def approve_all(_: Proposal) -> bool:
    return True


class Investigation:
    def __init__(
        self,
        backend: ClusterBackend,
        brain: Brain,
        approver: Approver = deny_all,
        listener: Listener | None = None,
        max_steps: int = 16,
    ) -> None:
        self.backend = backend
        self.brain = brain
        self.approver = approver
        self.listener = listener
        self.max_steps = max_steps
        self.tools = ToolRegistry(backend)
        self.events: list[Event] = []
        self.report: Report | None = None
        self.proposal: Proposal | None = None
        self.approved: bool | None = None
        self.action_result: dict[str, Any] | None = None
        self.tool_calls = 0

    def _emit(self, kind: str, **data: Any) -> None:
        event = Event(kind, data)
        self.events.append(event)
        if self.listener:
            self.listener(event)

    def run(self) -> Report:
        alert = self.backend.alert()
        self._emit("alert", **alert)
        self.brain.start(build_task(alert), alert, ALL_TOOLS)

        results: list[ToolResult] = []
        nudged = False
        nudge: str | None = None
        for _ in range(self.max_steps):
            try:
                step = self.brain.step(results, nudge)
            except BrainError as e:
                self._emit("error", message=str(e))
                break
            nudge = None
            if step.thought:
                self._emit("thought", text=step.thought)
            results = []

            if not step.calls:
                if nudged:
                    self._emit("error", message="The agent stopped without submitting a report.")
                    break
                nudged = True
                nudge = NUDGE
                continue

            for call in step.calls:
                self.tool_calls += 1
                self._emit("tool_call", id=call.id, name=call.name, input=call.input)
                if call.name == PROPOSE_TOOL["name"]:
                    content, is_error = self._handle_proposal(call.input)
                elif call.name == REPORT_TOOL["name"]:
                    self.report = Report.from_input(call.input)
                    content, is_error = "Report received.", False
                else:
                    content, is_error = self.tools.run(call.name, call.input)
                self._emit("tool_result", id=call.id, name=call.name, content=content, is_error=is_error)
                results.append(ToolResult(call.id, call.name, content, is_error))

            if self.report:
                break

        if self.report is None:
            self.report = Report(
                root_cause="The investigation ended without a conclusion.",
                category="Unknown",
                confidence=0.0,
                evidence=[],
                remediation="Escalate to a human on-call engineer.",
                follow_ups=[],
            )
        self.report.status = self._status()
        self._emit("report", **self.report.__dict__)
        return self.report

    def _status(self) -> str:
        if self.proposal is None:
            return "diagnosed"
        if not self.approved:
            return "awaiting_human"
        if self.action_result and self.action_result.get("symptoms_resolved"):
            return "resolved"
        return "diagnosed"

    def _handle_proposal(self, data: dict[str, Any]) -> tuple[str, bool]:
        if self.proposal is not None:
            return "Error: a remediation was already proposed in this investigation. Submit your report.", True
        proposal = Proposal.from_input(data)
        problem = proposal.validate()
        if problem:
            return f"Error: proposal rejected by policy: {problem}", True

        self.proposal = proposal
        self._emit("approval_request", **proposal.__dict__, summary=proposal.describe())
        try:
            approved = bool(self.approver(proposal))
        except Exception as e:  # an approver that crashes must never mean "approved"
            self._emit("error", message=f"approver failed: {e}")
            approved = False
        self.approved = approved
        self._emit("approval", approved=approved, summary=proposal.describe())

        if not approved:
            payload = {"approved": False, "result": None, "message": "A human rejected this change. Do not propose another; submit your report."}
            return json.dumps(payload), False

        try:
            result = self.backend.apply(proposal)
        except Exception as e:
            self._emit("error", message=f"applying the change failed: {e}")
            return json.dumps({"approved": True, "result": {"applied": False, "error": str(e)}}), True
        self.action_result = result
        self._emit("action_result", **result)
        return json.dumps({"approved": True, "result": result}), False

    def trace(self) -> dict[str, Any]:
        return {
            "brain": getattr(self.brain, "name", "unknown"),
            "tool_calls": self.tool_calls,
            "events": [e.to_dict() for e in self.events],
        }
