"""A brain decides what to look at next. The agent loop does everything else."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import Step, ToolResult


class BrainError(Exception):
    pass


class Brain(Protocol):
    name: str

    def start(self, task: str, alert: dict[str, Any], tools: list[dict[str, Any]]) -> None:
        """Begin a new investigation."""

    def step(self, results: list[ToolResult], nudge: str | None = None) -> Step:
        """Receive the results of the previous step's tool calls and decide the next step."""
