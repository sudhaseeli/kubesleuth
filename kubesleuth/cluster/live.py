"""A backend for a real cluster, using the official Kubernetes client and Prometheus.

Reads need only the read-only ClusterRole in ``deploy/rbac.yaml``. Writes are
off unless the backend is created with ``allow_writes=True`` *and* a human
approved the proposal, and even then they need the separate remediation Role.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..models import Proposal
from .base import NotFound

PROMQL = {
    "memory_working_set": 'sum(container_memory_working_set_bytes{{namespace="{ns}",pod=~"{t}-.*",container!=""}}) / 1048576',
    "memory_limit": 'sum(kube_pod_container_resource_limits{{namespace="{ns}",pod=~"{t}-.*",resource="memory"}}) / count(kube_pod_info{{namespace="{ns}",pod=~"{t}-.*"}}) / 1048576',
    "cpu_usage": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",pod=~"{t}-.*",container!=""}}[5m]))',
    "restarts": 'sum(kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"{t}-.*"}})',
    "http_5xx_rate": '100 * sum(rate(http_requests_total{{namespace="{ns}",service="{t}",code=~"5.."}}[5m])) / sum(rate(http_requests_total{{namespace="{ns}",service="{t}"}}[5m]))',
    "http_p99_latency": '1000 * histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{namespace="{ns}",service="{t}"}}[5m])))',
    "node_disk_usage": '100 * (1 - node_filesystem_avail_bytes{{mountpoint="/",instance=~"{t}.*"}} / node_filesystem_size_bytes{{mountpoint="/",instance=~"{t}.*"}})',
}

UNITS = {"memory_working_set": "MiB", "memory_limit": "MiB", "cpu_usage": "cores", "restarts": "count",
         "http_5xx_rate": "%", "http_p99_latency": "ms", "node_disk_usage": "%"}


def _window_seconds(window: str) -> int:
    try:
        n, unit = int(window[:-1]), window[-1]
        return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    except (ValueError, KeyError, IndexError):
        return 3600


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if hasattr(dt, "isoformat") else dt


class LiveCluster:
    def __init__(self, alert: dict[str, Any], prometheus_url: str | None = None, allow_writes: bool = False):
        try:
            from kubernetes import client, config
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise RuntimeError("The live backend needs the Kubernetes client: pip install 'kubesleuth[live]'") from e
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._client = client
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self._alert = alert
        self.prometheus_url = (prometheus_url or os.environ.get("PROMETHEUS_URL", "")).rstrip("/")
        self.allow_writes = allow_writes

    def alert(self) -> dict[str, Any]:
        return self._alert

    # -- helpers -----------------------------------------------------------

    def _owner(self, pod: Any) -> str | None:
        for ref in pod.metadata.owner_references or []:
            if ref.kind == "ReplicaSet":
                rs_name = ref.name
                dep = rs_name.rsplit("-", 1)[0]
                return f"deployment/{dep}"
            return f"{ref.kind.lower()}/{ref.name}"
        return None

    @staticmethod
    def _state(state: Any) -> dict[str, Any]:
        if state is None:
            return {}
        if state.waiting:
            return {"waiting": {"reason": state.waiting.reason, "message": state.waiting.message}}
        if state.terminated:
            t = state.terminated
            return {"terminated": {"reason": t.reason, "exit_code": t.exit_code, "finished_at": _iso(t.finished_at)}}
        if state.running:
            return {"running": {"started_at": _iso(state.running.started_at)}}
        return {}

    # -- reads -------------------------------------------------------------

    def list_pods(self, namespace: str, owner: str | None) -> list[dict[str, Any]]:
        rows = []
        for pod in self.core.list_namespaced_pod(namespace).items:
            pod_owner = self._owner(pod)
            if owner and pod_owner != owner:
                continue
            statuses = pod.status.container_statuses or []
            ready = sum(1 for s in statuses if s.ready)
            restarts = sum(s.restart_count for s in statuses)
            first = statuses[0] if statuses else None
            state = self._state(first.state) if first else {}
            last = self._state(first.last_state) if first else {}
            reason = next(iter(state.values()), {}).get("reason", "") if "running" not in state else ""
            rows.append({
                "name": pod.metadata.name, "owner": pod_owner, "phase": pod.status.phase,
                "ready": f"{ready}/{len(pod.spec.containers)}", "restarts": restarts, "node": pod.spec.node_name,
                "reason": reason or pod.status.reason or "", "last_termination": last.get("terminated", {}).get("reason", ""),
            })
        return rows

    def describe_pod(self, namespace: str, name: str) -> dict[str, Any]:
        from kubernetes.client.exceptions import ApiException
        try:
            pod = self.core.read_namespaced_pod(name, namespace)
        except ApiException as e:
            if e.status == 404:
                raise NotFound(f"pod {namespace}/{name}") from e
            raise
        statuses = {s.name: s for s in pod.status.container_statuses or []}
        containers = []
        for c in pod.spec.containers:
            s = statuses.get(c.name)
            probe = c.readiness_probe
            containers.append({
                "name": c.name, "image": c.image,
                "state": self._state(s.state) if s else {},
                "last_state": self._state(s.last_state) if s else {},
                "resources": {"requests": (c.resources.requests or {}) if c.resources else {}, "limits": (c.resources.limits or {}) if c.resources else {}},
                "readiness_probe": {"http_get": probe.http_get.path, "port": probe.http_get.port} if probe and probe.http_get else None,
            })
        return {
            "name": name, "namespace": namespace, "owner": self._owner(pod), "phase": pod.status.phase,
            "node": pod.spec.node_name, "reason": pod.status.reason, "message": pod.status.message,
            "conditions": {c.type: c.status for c in pod.status.conditions or []},
            "containers": containers,
        }

    def pod_logs(self, namespace: str, name: str, previous: bool, tail: int) -> dict[str, Any]:
        from kubernetes.client.exceptions import ApiException
        try:
            text = self.core.read_namespaced_pod_log(name, namespace, previous=previous, tail_lines=tail)
        except ApiException as e:
            if e.status in (400, 404):
                return {"pod": name, "previous": previous, "lines": [], "note": e.reason}
            raise
        return {"pod": name, "previous": previous, "lines": (text or "").splitlines()}

    def events(self, namespace: str, obj: str | None) -> list[dict[str, Any]]:
        selector = f"involvedObject.name={obj}" if obj else None
        items = self.core.list_namespaced_event(namespace, field_selector=selector).items
        items.sort(key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return [{
            "type": e.type, "reason": e.reason, "object": f"{e.involved_object.kind.lower()}/{e.involved_object.name}",
            "message": e.message, "count": e.count, "last_seen": _iso(e.last_timestamp or e.event_time),
        } for e in items[:30]]

    def deployment(self, namespace: str, name: str) -> dict[str, Any]:
        from kubernetes.client.exceptions import ApiException
        try:
            d = self.apps.read_namespaced_deployment(name, namespace)
        except ApiException as e:
            if e.status == 404:
                raise NotFound(f"deployment {namespace}/{name}") from e
            raise
        history = []
        selector = ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())
        for rs in self.apps.list_namespaced_replica_set(namespace, label_selector=selector).items:
            ann = rs.metadata.annotations or {}
            if "deployment.kubernetes.io/revision" not in ann:
                continue
            history.append({
                "revision": int(ann["deployment.kubernetes.io/revision"]),
                "image": rs.spec.template.spec.containers[0].image,
                "change_cause": ann.get("kubernetes.io/change-cause", ""),
                "rolled_out_at": _iso(rs.metadata.creation_timestamp),
            })
        history.sort(key=lambda h: h["revision"])
        return {
            "name": name, "namespace": namespace, "replicas": d.spec.replicas,
            "ready": d.status.ready_replicas or 0, "updated": d.status.updated_replicas or 0,
            "image": d.spec.template.spec.containers[0].image,
            "containers": [{"name": c.name, "limits": (c.resources.limits or {}) if c.resources else {}} for c in d.spec.template.spec.containers],
            "history": history[-10:],
        }

    def metric(self, metric: str, namespace: str, target: str, window: str) -> dict[str, Any]:
        if not self.prometheus_url:
            raise NotFound("Prometheus is not configured; set PROMETHEUS_URL or pass --prometheus")
        query = PROMQL[metric].format(ns=namespace, t=target)
        end = datetime.now(timezone.utc).timestamp()
        span = _window_seconds(window)
        params = urllib.parse.urlencode({"query": query, "start": end - span, "end": end, "step": max(span // 30, 15)})
        with urllib.request.urlopen(f"{self.prometheus_url}/api/v1/query_range?{params}", timeout=15) as resp:
            body = json.load(resp)
        result = body.get("data", {}).get("result", [])
        if not result:
            raise NotFound(f"no data for {metric} on {target}")
        points = [[datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%M"), round(float(v), 3)] for ts, v in result[0]["values"]]
        values = [p[1] for p in points]
        return {"metric": metric, "target": target, "window": window, "unit": UNITS[metric], "points": points,
                "summary": {"min": min(values), "max": max(values), "last": values[-1]}, "promql": query}

    def nodes(self, name: str | None) -> list[dict[str, Any]]:
        items = [self.core.read_node(name)] if name else self.core.list_node().items
        return [{
            "name": n.metadata.name,
            "conditions": {c.type: c.status for c in n.status.conditions or []},
            "allocatable": n.status.allocatable,
            "taints": [f"{t.key}:{t.effect}" for t in n.spec.taints or []],
        } for n in items]

    # -- writes ------------------------------------------------------------

    def apply(self, proposal: Proposal) -> dict[str, Any]:
        if not self.allow_writes:
            return {"applied": False, "change": proposal.describe(), "symptoms_resolved": False,
                    "verification": ["Writes are disabled. Re-run with --allow-writes to let approved changes run."],
                    "summary": "Approved, but not applied because writes are disabled."}
        ns, target, p = proposal.namespace, proposal.target, proposal.params
        if proposal.action == "restart_deployment":
            stamp = datetime.now(timezone.utc).isoformat()
            body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
            self.apps.patch_namespaced_deployment(target, ns, body)
        elif proposal.action == "scale_deployment":
            self.apps.patch_namespaced_deployment_scale(target, ns, {"spec": {"replicas": p["replicas"]}})
        elif proposal.action == "patch_memory_limit":
            d = self.apps.read_namespaced_deployment(target, ns)
            name = p.get("container") or d.spec.template.spec.containers[0].name
            body = {"spec": {"template": {"spec": {"containers": [{"name": name, "resources": {"limits": {"memory": p["memory"]}}}]}}}}
            self.apps.patch_namespaced_deployment(target, ns, body)
        elif proposal.action == "cordon_node":
            self.core.patch_node(target, {"spec": {"unschedulable": True}})
        elif proposal.action == "rollback_deployment":
            self._rollback(ns, target, p.get("to_revision"))
        return {"applied": True, "change": proposal.describe(), "symptoms_resolved": False,
                "verification": ["Change applied. Re-check the alert and metrics in a few minutes to confirm."],
                "summary": "Applied; verification on a live cluster is left to the follow-up check."}

    def _rollback(self, ns: str, name: str, to_revision: int | None) -> None:
        d = self.apps.read_namespaced_deployment(name, ns)
        selector = ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())
        sets = []
        for rs in self.apps.list_namespaced_replica_set(ns, label_selector=selector).items:
            rev = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
            if rev:
                sets.append((int(rev), rs))
        sets.sort(key=lambda x: x[0])
        if len(sets) < 2 and to_revision is None:
            raise RuntimeError("no previous revision to roll back to")
        target_rs = next((rs for rev, rs in sets if rev == to_revision), None) if to_revision else sets[-2][1]
        if target_rs is None:
            raise RuntimeError(f"revision {to_revision} not found")
        template = self._client.ApiClient().sanitize_for_serialization(target_rs.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        self.apps.patch_namespaced_deployment(name, ns, {"spec": {"template": template}})
