"""A cluster backend that replays a recorded incident scenario.

Scenarios live in ``kubesleuth/scenarios/*.json``. They let you run, test and
demo the agent end to end without a real cluster, and they double as an eval
set because each one records its ground-truth root cause.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from typing import Any

from ..models import Proposal
from .base import NotFound


def _scenario_files():
    return sorted(
        (p for p in resources.files("kubesleuth.scenarios").iterdir() if p.name.endswith(".json")),
        key=lambda p: p.name,
    )


def list_scenarios() -> list[dict[str, str]]:
    out = []
    for path in _scenario_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"id": data["id"], "title": data["title"], "category": data["ground_truth"]["category"]})
    return out


def load_scenario(scenario_id: str) -> dict[str, Any]:
    for path in _scenario_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["id"] == scenario_id:
            return data
    known = ", ".join(s["id"] for s in list_scenarios())
    raise KeyError(f"unknown scenario '{scenario_id}'. Known scenarios: {known}")


def _summarise(points: list[list[Any]]) -> dict[str, Any]:
    values = [p[1] for p in points]
    if not values:
        return {}
    first, last = values[0], values[-1]
    trend = "flat"
    if last > first * 1.2 + 1e-9:
        trend = "rising"
    elif last < first * 0.8 - 1e-9:
        trend = "falling"
    return {"min": min(values), "max": max(values), "last": last, "trend": trend}


class SimulatedCluster:
    def __init__(self, scenario: dict[str, Any] | str):
        self.scenario = load_scenario(scenario) if isinstance(scenario, str) else copy.deepcopy(scenario)
        self.applied: list[Proposal] = []
        self.resolved = False

    # -- reads -------------------------------------------------------------

    def alert(self) -> dict[str, Any]:
        return self.scenario["alert"]

    def _pods(self, namespace: str) -> list[dict[str, Any]]:
        return [p for p in self.scenario.get("pods", []) if p["namespace"] == namespace]

    def list_pods(self, namespace: str, owner: str | None) -> list[dict[str, Any]]:
        rows = []
        for p in self._pods(namespace):
            if owner and p.get("owner") != owner:
                continue
            c = p["containers"][0]
            state = c.get("state", {})
            reason = ""
            if "waiting" in state:
                reason = state["waiting"].get("reason", "")
            elif "terminated" in state:
                reason = state["terminated"].get("reason", "")
            last = c.get("last_state", {}).get("terminated", {})
            rows.append(
                {
                    "name": p["name"],
                    "owner": p.get("owner"),
                    "phase": p["phase"],
                    "ready": p["ready"],
                    "restarts": p.get("restarts", 0),
                    "node": p.get("node"),
                    "reason": reason or p.get("reason", ""),
                    "last_termination": last.get("reason", ""),
                }
            )
        return rows

    def describe_pod(self, namespace: str, name: str) -> dict[str, Any]:
        for p in self._pods(namespace):
            if p["name"] == name:
                return p
        raise NotFound(f"pod {namespace}/{name}")

    def pod_logs(self, namespace: str, name: str, previous: bool, tail: int) -> dict[str, Any]:
        self.describe_pod(namespace, name)
        logs = self.scenario.get("logs", {}).get(name)
        if logs is None:
            raise NotFound(f"no logs recorded for pod {namespace}/{name}")
        key = "previous" if previous else "current"
        text = logs.get(key)
        if text is None:
            return {"pod": name, "previous": previous, "lines": [], "note": f"no {key} container instance"}
        lines = text.splitlines()[-tail:]
        return {"pod": name, "previous": previous, "lines": lines}

    def events(self, namespace: str, obj: str | None) -> list[dict[str, Any]]:
        out = []
        for e in self.scenario.get("events", []):
            if e.get("namespace", namespace) != namespace:
                continue
            if obj and obj not in e["object"]:
                continue
            out.append(e)
        return out

    def deployment(self, namespace: str, name: str) -> dict[str, Any]:
        for d in self.scenario.get("deployments", []):
            if d["namespace"] == namespace and d["name"] == name:
                return d
        raise NotFound(f"deployment {namespace}/{name}")

    def metric(self, metric: str, namespace: str, target: str, window: str) -> dict[str, Any]:
        series = self.scenario.get("metrics", {}).get(f"{metric}:{target}")
        if series is None:
            raise NotFound(f"no '{metric}' series for '{target}' in namespace '{namespace}'")
        return {
            "metric": metric,
            "target": target,
            "window": window,
            "unit": series.get("unit", ""),
            "points": series["points"],
            "summary": _summarise(series["points"]),
        }

    def nodes(self, name: str | None) -> list[dict[str, Any]]:
        nodes = self.scenario.get("nodes", [])
        if name:
            nodes = [n for n in nodes if n["name"] == name]
            if not nodes:
                raise NotFound(f"node {name}")
        return nodes

    # -- writes ------------------------------------------------------------

    def apply(self, proposal: Proposal) -> dict[str, Any]:
        """Simulate applying an approved remediation and report what changed."""
        self.applied.append(proposal)
        resolution = self.scenario.get("resolution", {})
        for accepted in resolution.get("accepts", []):
            if accepted["action"] == proposal.action and accepted["target"] == proposal.target:
                self.resolved = True
                after = resolution.get("after", {})
                return {
                    "applied": True,
                    "change": proposal.describe(),
                    "verification": after.get("checks", []),
                    "symptoms_resolved": True,
                    "summary": after.get("summary", "Symptoms cleared after the change."),
                }
        return {
            "applied": True,
            "change": proposal.describe(),
            "verification": [resolution.get("wrong_fix", "The alert is still firing after the change.")],
            "symptoms_resolved": False,
            "summary": "The change was applied but the incident is not resolved.",
        }
