"""Core domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass(slots=True)
class Issue:
    """Normalized issue model used across orchestration."""

    id: str
    identifier: str
    title: str
    description: Optional[str]
    priority: Optional[int]
    state: str
    branch_name: Optional[str] = None
    url: Optional[str] = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[dict[str, Optional[str]]] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    assignee_id: Optional[str] = None
    assigned_to_worker: bool = True

    def template_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "state": self.state,
            "branch_name": self.branch_name,
            "url": self.url,
            "labels": list(self.labels),
            "blocked_by": list(self.blocked_by),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "assignee_id": self.assignee_id,
            "assigned_to_worker": self.assigned_to_worker,
        }


@dataclass(slots=True)
class WorkflowDefinition:
    """In-memory workflow document."""

    path: Path
    config: dict[str, Any]
    prompt_template: str
    loaded_at: datetime
    mtime_ns: int


@dataclass(slots=True)
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_monotonic_ms: int
    error: Optional[str] = None


@dataclass(slots=True)
class CodexUsageDelta:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: int = 0

    def apply(self, delta: CodexUsageDelta) -> None:
        self.input_tokens = max(0, self.input_tokens + delta.input_tokens)
        self.output_tokens = max(0, self.output_tokens + delta.output_tokens)
        self.total_tokens = max(0, self.total_tokens + delta.total_tokens)


@dataclass(slots=True)
class RunningEntry:
    issue: Issue
    issue_id: str
    identifier: str
    workspace_path: Path
    future: Any
    cancel_event: Any
    retry_attempt: int
    started_at: datetime
    last_codex_timestamp: Optional[datetime] = None
    session_id: Optional[str] = None
    codex_app_server_pid: Optional[str] = None
    last_codex_event: Optional[str] = None
    last_codex_message: Optional[str] = None
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    codex_last_reported_input_tokens: int = 0
    codex_last_reported_output_tokens: int = 0
    codex_last_reported_total_tokens: int = 0
    turn_count: int = 0


@dataclass(slots=True)
class WorkerResult:
    issue_id: str
    identifier: str
    started_at: datetime
    ended_at: datetime
    success: bool
    reason: Optional[str] = None

