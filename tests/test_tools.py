import json

from kubesleuth.cluster.simulated import SimulatedCluster
from kubesleuth.tools import ALL_TOOLS, ToolRegistry


def registry(scenario="oom-kill"):
    return ToolRegistry(SimulatedCluster(scenario))


def test_tool_schemas_are_well_formed():
    names = [t["name"] for t in ALL_TOOLS]
    assert len(names) == len(set(names))
    for tool in ALL_TOOLS:
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"])
        assert schema.get("additionalProperties") is False
        assert len(tool["description"]) > 30


def test_list_pods_filters_by_owner():
    content, is_error = registry().run("list_pods", {"namespace": "shop", "owner": "deployment/checkout-api"})
    pods = json.loads(content)
    assert not is_error
    assert len(pods) == 3 and all(p["owner"] == "deployment/checkout-api" for p in pods)
    assert {p["last_termination"] for p in pods} == {"OOMKilled"}


def test_previous_logs_contain_the_crash():
    content, _ = registry().run("get_pod_logs", {"namespace": "shop", "name": "checkout-api-7d9f8b6c5-8rmvd", "previous": True})
    assert any("RSS 252 MiB" in line for line in json.loads(content)["lines"])


def test_errors_are_returned_not_raised():
    r = registry()
    content, is_error = r.run("describe_pod", {"namespace": "shop", "name": "nope"})
    assert is_error and content.startswith("Not found")
    content, is_error = r.run("describe_pod", {"namespace": "shop"})
    assert is_error and "'name' is required" in content
    content, is_error = r.run("query_metric", {"metric": "made_up", "namespace": "shop", "target": "x"})
    assert is_error and "unknown metric" in content


def test_registry_refuses_write_tools():
    content, is_error = registry().run("propose_remediation", {"action": "cordon_node"})
    assert is_error and "read-only" in content


def test_metric_summary():
    content, _ = registry().run("query_metric", {"metric": "memory_working_set", "namespace": "shop", "target": "checkout-api"})
    summary = json.loads(content)["summary"]
    assert summary == {"min": 118, "max": 255, "last": 254, "trend": "rising"}
