"""The interface every cluster backend implements (simulated or live)."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import Proposal


class NotFound(Exception):
    pass


class ClusterBackend(Protocol):
    """Read methods return JSON-serialisable data. ``apply`` is only ever called
    after a human approved the proposal."""

    def alert(self) -> dict[str, Any]: ...

    def list_pods(self, namespace: str, owner: str | None) -> list[dict[str, Any]]: ...

    def describe_pod(self, namespace: str, name: str) -> dict[str, Any]: ...

    def pod_logs(self, namespace: str, name: str, previous: bool, tail: int) -> dict[str, Any]: ...

    def events(self, namespace: str, obj: str | None) -> list[dict[str, Any]]: ...

    def deployment(self, namespace: str, name: str) -> dict[str, Any]: ...

    def metric(self, metric: str, namespace: str, target: str, window: str) -> dict[str, Any]: ...

    def nodes(self, name: str | None) -> list[dict[str, Any]]: ...

    def apply(self, proposal: Proposal) -> dict[str, Any]: ...
