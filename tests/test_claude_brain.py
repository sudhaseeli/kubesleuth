"""Tests for the Claude brain using a fake client, so no API key or network is needed."""

from types import SimpleNamespace as NS

import pytest

from kubesleuth.agent import Investigation, approve_all
from kubesleuth.brains.base import BrainError
from kubesleuth.brains.claude import ClaudeBrain
from kubesleuth.cluster.simulated import SimulatedCluster


def tool_use(_id, _name, /, **input):
    return NS(type="tool_use", id=_id, name=_name, input=input)


def text(t):
    return NS(type="text", text=t)


def response(*content, stop="tool_use"):
    return NS(content=list(content), stop_reason=stop, stop_details=None,
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=80))


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        # Copy the message list: the brain keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def fake_client(responses):
    messages = FakeMessages(responses)
    return NS(messages=messages, beta=NS(messages=messages)), messages


REPORT = dict(root_cause="Memory limit too low after cache change", category="OOMKilled", confidence=0.9,
              evidence=["OOMKilled exit 137"], remediation="Raised the limit", follow_ups=["Bound the cache"])


def test_claude_brain_drives_a_full_investigation():
    client, api = fake_client([
        response(text("Checking pods first."), tool_use("t1", "list_pods", namespace="shop", owner="deployment/checkout-api")),
        response(tool_use("t2", "describe_pod", namespace="shop", name="checkout-api-7d9f8b6c5-8rmvd"),
                 tool_use("t3", "get_pod_logs", namespace="shop", name="checkout-api-7d9f8b6c5-8rmvd", previous=True)),
        response(tool_use("t4", "propose_remediation", action="patch_memory_limit", namespace="shop", target="checkout-api",
                          params={"container": "checkout-api", "memory": "512Mi"}, rationale="Working set hits the limit.", risk="low")),
        response(tool_use("t5", "submit_report", **REPORT)),
    ])
    backend = SimulatedCluster("oom-kill")
    brain = ClaudeBrain(client=client)
    report = Investigation(backend, brain, approver=approve_all).run()

    assert report.category == "OOMKilled" and report.status == "resolved"
    assert backend.resolved
    assert len(api.requests) == 4

    first = api.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["fallbacks"] == "default"
    assert {t["name"] for t in first["tools"]} >= {"list_pods", "propose_remediation", "submit_report"}

    # Parallel tool calls come back as tool_result blocks in a single user message.
    third = api.requests[2]["messages"]
    results = third[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t2", "t3"]
    assert all(r["type"] == "tool_result" for r in results)

    # History is append-only: each request extends the previous one.
    for prev, nxt in zip(api.requests, api.requests[1:]):
        assert nxt["messages"][: len(prev["messages"])] == prev["messages"]
    assert brain.usage["cache_read_input_tokens"] == 320


def test_claude_brain_gets_a_nudge_when_it_stops_without_a_report():
    client, api = fake_client([
        response(text("I think it's memory."), stop="end_turn"),
        response(tool_use("t1", "submit_report", **REPORT)),
    ])
    report = Investigation(SimulatedCluster("oom-kill"), ClaudeBrain(client=client)).run()
    assert report.category == "OOMKilled"
    nudge = api.requests[1]["messages"][-1]["content"][-1]
    assert nudge["type"] == "text" and "submit_report" in nudge["text"]


def test_refusal_is_surfaced_as_an_error():
    refused = NS(content=[], stop_reason="refusal", stop_details=NS(category="cyber"), usage=None)
    client, _ = fake_client([refused])
    brain = ClaudeBrain(client=client)
    brain.start("task", {}, [])
    with pytest.raises(BrainError, match="declined"):
        brain.step([])
