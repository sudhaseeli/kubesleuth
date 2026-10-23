import json

import pytest

from kubesleuth.agent import Investigation, approve_all, deny_all
from kubesleuth.brains.rules import RuleBrain
from kubesleuth.cluster.simulated import SimulatedCluster, list_scenarios, load_scenario
from kubesleuth.models import Proposal, Step, ToolCall

SCENARIOS = [s["id"] for s in list_scenarios()]


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_rule_brain_diagnoses_and_resolves_every_scenario(scenario_id):
    truth = load_scenario(scenario_id)["ground_truth"]
    backend = SimulatedCluster(scenario_id)
    inv = Investigation(backend, RuleBrain(), approver=approve_all)
    report = inv.run()

    assert report.category == truth["category"]
    assert inv.proposal is not None and inv.proposal.action == truth["action"]
    assert backend.resolved
    assert report.status == "resolved"
    assert report.evidence, "a report must cite evidence"
    assert inv.tool_calls <= 12


def test_rejected_fix_is_never_applied():
    backend = SimulatedCluster("oom-kill")
    inv = Investigation(backend, RuleBrain(), approver=deny_all)
    report = inv.run()

    assert backend.applied == []
    assert report.status == "awaiting_human"
    assert "not applied" in report.remediation
    kinds = [e.kind for e in inv.events]
    assert "approval_request" in kinds and "action_result" not in kinds


def test_crashing_approver_counts_as_rejection():
    def broken(_):
        raise RuntimeError("pager integration down")

    backend = SimulatedCluster("crashloop-config")
    inv = Investigation(backend, RuleBrain(), approver=broken)
    inv.run()
    assert backend.applied == []
    assert inv.approved is False


class ScriptedBrain:
    """Replays fixed steps, to test the loop's guardrails."""

    name = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    def start(self, task, alert, tools):
        self.task = task

    def step(self, results, nudge=None):
        self.seen.append((results, nudge))
        return self.steps.pop(0) if self.steps else Step()


def test_policy_blocks_disallowed_actions_and_protected_namespaces():
    steps = [
        Step(calls=[ToolCall("a", "propose_remediation", {"action": "delete_namespace", "namespace": "shop", "target": "x", "params": {}, "rationale": "", "risk": "high"})]),
        Step(calls=[ToolCall("b", "propose_remediation", {"action": "restart_deployment", "namespace": "kube-system", "target": "coredns", "params": {}, "rationale": "", "risk": "low"})]),
        Step(calls=[ToolCall("c", "submit_report", {"root_cause": "x", "category": "Unknown", "confidence": 0.1, "evidence": [], "remediation": "", "follow_ups": []})]),
    ]
    backend = SimulatedCluster("oom-kill")
    approvals = []
    inv = Investigation(backend, ScriptedBrain(steps), approver=lambda p: approvals.append(p) or True)
    inv.run()

    errors = [e.data for e in inv.events if e.kind == "tool_result" and e.data["is_error"]]
    assert len(errors) == 2
    assert "not allowed" in errors[0]["content"] and "kube-system" in errors[1]["content"]
    assert approvals == [] and backend.applied == []


def test_only_one_remediation_per_investigation():
    fix = {"action": "restart_deployment", "namespace": "shop", "target": "checkout-api", "params": {}, "rationale": "r", "risk": "low"}
    steps = [
        Step(calls=[ToolCall("a", "propose_remediation", dict(fix))]),
        Step(calls=[ToolCall("b", "propose_remediation", dict(fix))]),
        Step(calls=[ToolCall("c", "submit_report", {"root_cause": "x", "category": "OOMKilled", "confidence": 0.5, "evidence": ["e"], "remediation": "r", "follow_ups": []})]),
    ]
    backend = SimulatedCluster("oom-kill")
    inv = Investigation(backend, ScriptedBrain(steps), approver=approve_all)
    inv.run()
    assert len(backend.applied) == 1


def test_agent_nudges_once_then_gives_up():
    brain = ScriptedBrain([Step(thought="hmm"), Step(thought="still thinking")])
    inv = Investigation(SimulatedCluster("oom-kill"), brain, approver=deny_all)
    report = inv.run()
    assert brain.seen[1][1] is not None, "second step should carry the nudge"
    assert report.category == "Unknown"
    assert any(e.kind == "error" for e in inv.events)


def test_proposal_validation():
    ok = Proposal("patch_memory_limit", "shop", "checkout-api", {"memory": "512Mi"}, "r", "low")
    assert ok.validate() is None
    assert "memory" in Proposal("patch_memory_limit", "shop", "checkout-api", {}, "r", "low").validate()
    assert "integer" in Proposal("scale_deployment", "shop", "checkout-api", {"replicas": "3"}, "r", "low").validate()


def test_trace_is_json_serialisable():
    inv = Investigation(SimulatedCluster("image-pull"), RuleBrain(), approver=approve_all)
    inv.run()
    json.dumps(inv.trace())
