"""A deterministic SRE playbook brain.

It needs no API key, so the project runs, tests and demos offline. It follows
the same tools and approval gate as the Claude brain, which also makes it a
useful baseline when evaluating the LLM: the Claude brain should match it on
the bundled scenarios and beat it on anything the playbook does not cover.
"""

from __future__ import annotations

import json
import re
from typing import Any, Generator

from ..models import Step, ToolCall, ToolResult

PlaybookGen = Generator[Step, list[ToolResult], None]

_ERROR_LINE = re.compile(r"\b(FATAL|ERROR|panic|Exception|404|502|timeout)\b", re.IGNORECASE)


def _load(result: ToolResult) -> Any:
    if result.is_error:
        return None
    try:
        return json.loads(result.content)
    except json.JSONDecodeError:
        return None


def _parse_mi(value: str) -> int | None:
    m = re.fullmatch(r"(\d+)(Mi|Gi)", value or "")
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2) == "Gi" else n


def _key_log_lines(logs: dict[str, Any] | None, limit: int = 2) -> list[str]:
    if not logs:
        return []
    hits = [line for line in logs.get("lines", []) if _ERROR_LINE.search(line)]
    return hits[:limit]


class RuleBrain:
    name = "rules"

    def __init__(self) -> None:
        self._gen: PlaybookGen | None = None
        self._n = 0
        self.alert: dict[str, Any] = {}

    def start(self, task: str, alert: dict[str, Any], tools: list[dict[str, Any]]) -> None:
        self.alert = alert
        self._n = 0
        self._gen = self._playbook()
        self._first = True

    def step(self, results: list[ToolResult], nudge: str | None = None) -> Step:
        assert self._gen is not None, "call start() first"
        try:
            if self._first:
                self._first = False
                return next(self._gen)
            return self._gen.send(results)
        except StopIteration:
            return Step(thought="Investigation finished.")

    # -- helpers -----------------------------------------------------------

    def _call(self, _tool: str, /, **args: Any) -> ToolCall:
        self._n += 1
        return ToolCall(id=f"rule_{self._n}", name=_tool, input=args)

    # -- the playbook --------------------------------------------------------

    def _playbook(self) -> PlaybookGen:
        alert = self.alert
        ns = alert["namespace"]
        owner = alert["target"]
        workload = owner.split("/", 1)[-1]
        evidence: list[str] = []

        res = yield Step(
            f"{alert['name']} fired for {owner}: {alert['summary']} "
            f"First I'll see what state its pods are in.",
            [self._call("list_pods", namespace=ns, owner=owner)],
        )
        pods = _load(res[0]) or []
        if not pods:
            yield Step(
                "I couldn't find any pods for this workload, so I can't go further on my own.",
                [self._report("Unknown", f"No pods found for {owner}.", 0.2, ["list_pods returned no pods"], "Escalate to the service owner.", [])],
            )
            return

        def badness(p: dict[str, Any]) -> tuple:
            not_ready = p["ready"].split("/")[0] == "0"
            abnormal = p["phase"] != "Running" or bool(p["reason"])
            return (abnormal, not_ready, p["restarts"])

        worst = max(pods, key=badness)
        unhealthy = [p for p in pods if badness(p)[0] or badness(p)[1]]
        restarted = sum(1 for p in pods if p["restarts"] > 0)
        evidence.append(
            f"{len(unhealthy)} of {len(pods)} pods unhealthy"
            + (f" and {restarted} restarted" if restarted else "")
            + f"; worst is {worst['name']} "
            f"({worst['phase']}, ready {worst['ready']}, {worst['restarts']} restarts"
            + (f", {worst['reason']}" if worst["reason"] else "")
            + ")"
        )

        opening = (
            f"{len(unhealthy)} of {len(pods)} pods look unhealthy. I'll describe the worst one, "
            if unhealthy
            else f"All {len(pods)} pods are running and ready, so the problem may be above the pod level. I'll still describe one, "
        )
        res = yield Step(
            opening + f"{worst['name']}, and pull its recent events at the same time.",
            [
                self._call("describe_pod", namespace=ns, name=worst["name"]),
                self._call("get_events", namespace=ns, object=worst["name"]),
            ],
        )
        pod = _load(res[0]) or {}
        events = _load(res[1]) or []
        container = (pod.get("containers") or [{}])[0]
        waiting = container.get("state", {}).get("waiting", {}).get("reason", "")
        last = container.get("last_state", {}).get("terminated", {})
        last_reason = last.get("reason", "")
        event_text = " ".join(e.get("message", "") for e in events)

        if last_reason:
            evidence.append(f"last termination: {last_reason} (exit code {last.get('exit_code')})")
        if waiting:
            evidence.append(f"container waiting: {waiting}")

        previous = bool(last_reason) or worst["restarts"] > 0
        res = yield Step(
            "Next, the logs"
            + (" from the crashed instance" if previous else "")
            + f" and the rollout history of {workload}, to see whether a recent change lines up with this.",
            [
                self._call("get_pod_logs", namespace=ns, name=worst["name"], previous=previous, tail=50),
                self._call("get_deployment", namespace=ns, name=workload),
            ],
        )
        logs = _load(res[0])
        deployment = _load(res[1]) or {}
        history = deployment.get("history", [])
        latest = history[-1] if history else None
        prior = history[-2] if len(history) > 1 else None
        key_lines = _key_log_lines(logs)
        for line in key_lines:
            evidence.append(f"log: {line.strip()}")
        if latest:
            evidence.append(
                f"latest rollout: revision {latest['revision']} at {latest['rolled_out_at']} "
                f"(\"{latest['change_cause']}\")"
            )

        rollback = (
            self._call(
                "propose_remediation",
                action="rollback_deployment",
                namespace=ns,
                target=workload,
                params={"to_revision": prior["revision"]} if prior else {},
                rationale="",
                risk="low",
            )
            if prior
            else None
        )

        # -- classify ------------------------------------------------------
        if last_reason == "OOMKilled":
            res = yield Step(
                "The container was OOMKilled. I'll compare its memory working set with its limit to confirm "
                "it is genuinely running out of memory rather than leaking slowly.",
                [
                    self._call("query_metric", metric="memory_working_set", namespace=ns, target=workload, window="1h"),
                    self._call("query_metric", metric="memory_limit", namespace=ns, target=workload, window="1h"),
                ],
            )
            used = (_load(res[0]) or {}).get("summary", {})
            limit_now = container.get("resources", {}).get("limits", {}).get("memory", "")
            limit_mi = _parse_mi(limit_now) or int((_load(res[1]) or {}).get("summary", {}).get("last", 0)) or 256
            if used:
                evidence.append(
                    f"memory working set {used.get('trend')} to {used.get('max')} MiB against a {limit_mi} MiB limit"
                )
            new_limit = f"{limit_mi * 2}Mi"
            root = (
                f"The {workload} container is exceeding its {limit_now or str(limit_mi) + 'Mi'} memory limit and is OOMKilled. "
                + (
                    f"Memory jumped after revision {latest['revision']} (\"{latest['change_cause']}\")."
                    if latest
                    else ""
                )
            )
            proposal = self._call(
                "propose_remediation",
                action="patch_memory_limit",
                namespace=ns,
                target=workload,
                params={"container": container.get("name", workload), "memory": new_limit},
                rationale=f"Working set peaks near the {limit_mi} MiB limit; doubling it to {new_limit} stops the OOM kills while the cache change is reviewed.",
                risk="low",
            )
            yield from self._finish(
                "OOMKilled", root, 0.9, evidence, proposal,
                [
                    "Bound the in-memory cache or make the preload size configurable.",
                    "Add an alert on memory working set above 85% of the limit.",
                    "Load-test memory before rolling out cache changes.",
                ],
            )
            return

        if waiting in ("ImagePullBackOff", "ErrImagePull") or "ErrImagePull" in event_text:
            image = container.get("image", "")
            pull_error = next((e["message"] for e in events if "manifest" in e.get("message", "") or "not found" in e.get("message", "")), "")
            if pull_error:
                evidence.append(f"event: {pull_error}")
            root = (
                f"The new pods cannot pull image {image}; the registry says the tag does not exist, so the rollout of "
                f"revision {latest['revision'] if latest else '?'} is stuck while old pods keep serving."
            )
            if rollback:
                rollback.input["rationale"] = "The new image tag does not exist; rolling back restores a pullable image and unblocks the rollout."
            yield from self._finish(
                "ImagePullBackOff", root, 0.92, evidence, rollback,
                ["Fix the image tag in the release pipeline.", "Validate that image tags exist in the registry before deploying (admission check or CI step)."],
            )
            return

        if worst.get("reason") == "Evicted" or "low on resource" in event_text:
            node = worst.get("node")
            res = yield Step(
                f"The pod was evicted for low resources. I'll check node {node} and its disk usage.",
                [
                    self._call("get_node_status", name=node),
                    self._call("query_metric", metric="node_disk_usage", namespace=ns, target=node, window="2h"),
                    self._call("get_events", namespace=ns, object=node),
                ],
            )
            node_info = ((_load(res[0]) or [{}]) or [{}])[0]
            disk = (_load(res[1]) or {}).get("summary", {})
            node_events = _load(res[2]) or []
            conditions = node_info.get("conditions", {})
            evidence.append(f"node {node}: DiskPressure={conditions.get('DiskPressure')}, disk usage {disk.get('last')}% and {disk.get('trend')}")
            if node_info.get("notes"):
                evidence.append(f"node note: {node_info['notes']}")
            for e in node_events[:1]:
                evidence.append(f"event on {node}: {e['reason']}: {e['message']}")
            root = (
                f"Node {node} is under disk pressure ({disk.get('last')}% used, DiskPressure={conditions.get('DiskPressure')}), "
                f"so the kubelet keeps evicting {workload} pods scheduled there. The workload itself is healthy on other nodes."
            )
            proposal = self._call(
                "propose_remediation",
                action="cordon_node",
                namespace=ns,
                target=node,
                params={},
                rationale=f"Cordoning {node} stops new pods landing on the full disk; healthy nodes have plenty of space.",
                risk="low",
            )
            yield from self._finish(
                "NodePressure", root, 0.88, evidence, proposal,
                ["Clean up container logs on the node and set log rotation limits.", "Lower the log-shipper DaemonSet log level.", "Alert on node disk usage above 80%."],
            )
            return

        if "Readiness probe failed" in event_text:
            probe = container.get("readiness_probe", {})
            evidence.append(f"readiness probe: GET {probe.get('http_get')} on port {probe.get('port', '?')}")
            root = (
                f"Pods are running but fail their readiness probe on {probe.get('http_get')}, which the app answers with 404. "
                + (f"Revision {latest['revision']} changed the probe (\"{latest['change_cause']}\")." if latest else "")
            )
            if rollback:
                rollback.input["rationale"] = "The previous revision probed an endpoint the app serves; rolling back makes pods ready again."
            yield from self._finish(
                "ProbeFailure", root, 0.9, evidence, rollback,
                ["Add the new readiness endpoint to the app before switching probes.", "Use maxUnavailable < 100% so a bad probe can't take every replica out at once."],
            )
            return

        if waiting == "CrashLoopBackOff":
            root = (
                f"{workload} exits on startup and is crash-looping. "
                + (f"The crash log says: {key_lines[0].strip()} " if key_lines else "")
                + (f"This started with revision {latest['revision']} (\"{latest['change_cause']}\")." if latest else "")
            )
            if rollback:
                rollback.input["rationale"] = "The crash starts with the latest revision and the previous revision ran cleanly; rolling back restores service while the config is fixed."
            yield from self._finish(
                "CrashLoopBackOff", root, 0.88, evidence, rollback,
                ["Fix the configuration the latest revision broke.", "Validate required config at deploy time, not only at startup."],
            )
            return

        # Pods look healthy: check service-level symptoms.
        res = yield Step(
            "The pods themselves look healthy, so I'll check service-level metrics: error rate and latency.",
            [
                self._call("query_metric", metric="http_5xx_rate", namespace=ns, target=workload, window="1h"),
                self._call("query_metric", metric="http_p99_latency", namespace=ns, target=workload, window="1h"),
            ],
        )
        errors = _load(res[0]) or {}
        err = errors.get("summary", {})
        if err and err.get("last", 0) > 1 and latest:
            points = errors.get("points", [])
            jump = next((p for p in points if p[1] > 1), None)
            evidence.append(
                f"5xx rate {err.get('min')}% -> {err.get('last')}%"
                + (f", starting at {jump[0]}" if jump else "")
                + f", right after revision {latest['revision']} rolled out"
            )
            root = (
                f"The error rate rose from {err.get('min')}% to {err.get('last')}% immediately after revision {latest['revision']} "
                f"(\"{latest['change_cause']}\"). "
                + (f"Logs show: {key_lines[0].strip()}" if key_lines else "")
            )
            if rollback:
                rollback.input["rationale"] = "Errors began with the latest rollout and pods are otherwise healthy; rolling back is the fastest safe fix."
            yield from self._finish(
                "BadRollout", root, 0.85, evidence, rollback,
                ["Restore a timeout above the upstream's p99 latency, with retries.", "Gate rollouts on error-rate canary analysis."],
            )
            return

        yield Step(
            "I couldn't match this to a known failure pattern, so I'll hand over what I found.",
            [self._report("Unknown", "No known failure pattern matched.", 0.3, evidence, "Escalate to the service owner with the evidence above.", [])],
        )

    def _report(self, category: str, root: str, confidence: float, evidence: list[str], remediation: str, follow_ups: list[str]) -> ToolCall:
        return self._call(
            "submit_report",
            root_cause=root,
            category=category,
            confidence=confidence,
            evidence=list(evidence),
            remediation=remediation,
            follow_ups=follow_ups,
        )

    def _finish(
        self,
        category: str,
        root: str,
        confidence: float,
        evidence: list[str],
        proposal: ToolCall | None,
        follow_ups: list[str],
    ) -> PlaybookGen:
        if proposal is None:
            yield Step(
                "I know what is wrong but have no safe automatic fix to propose.",
                [self._report(category, root, confidence, evidence, "Needs a manual fix; see the root cause.", follow_ups)],
            )
            return
        res = yield Step(
            f"Root cause found ({category}). I'm proposing one fix; nothing changes until a human approves it.",
            [proposal],
        )
        outcome = _load(res[0]) if res else None
        desc = f"{proposal.input['action']} {proposal.input['namespace']}/{proposal.input['target']}"
        if outcome and outcome.get("approved") and outcome.get("result", {}).get("symptoms_resolved"):
            remediation = f"Applied {desc}. {outcome['result'].get('summary', '')}".strip()
            for check in outcome["result"].get("verification", []):
                evidence.append(f"after fix: {check}")
        elif outcome and outcome.get("approved"):
            remediation = f"Applied {desc}, but symptoms persist: {'; '.join(outcome.get('result', {}).get('verification', []))}"
            confidence = min(confidence, 0.5)
        else:
            remediation = f"Proposed {desc}; not applied because it was not approved."
        yield Step(
            "Writing up the incident report.",
            [self._report(category, root, confidence, evidence, remediation, follow_ups)],
        )
