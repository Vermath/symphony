"""Typed configuration derived from WORKFLOW.md."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import ConfigValidationError
from .models import WorkflowDefinition

DEFAULT_ACTIVE_STATES = ["Todo", "In Progress"]
DEFAULT_TERMINAL_STATES = ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
DEFAULT_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
DEFAULT_JIRA_ENDPOINT = None
DEFAULT_POLL_INTERVAL_MS = 30_000
DEFAULT_WORKSPACE_ROOT = Path(os.getenv("TMPDIR") or Path.cwd() / "symphony_workspaces")
DEFAULT_HOOK_TIMEOUT_MS = 60_000
DEFAULT_MAX_CONCURRENT_AGENTS = 10
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_RETRY_BACKOFF_MS = 300_000
DEFAULT_CODEX_COMMAND = "codex app-server"
DEFAULT_CODEX_TURN_TIMEOUT_MS = 3_600_000
DEFAULT_CODEX_READ_TIMEOUT_MS = 5_000
DEFAULT_CODEX_STALL_TIMEOUT_MS = 300_000
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_PROMPT_TEMPLATE = """You are working on an issue.

Identifier: {{ issue.identifier }}
Title: {{ issue.title }}

Body:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}
"""
DEFAULT_APPROVAL_POLICY: str | dict[str, object] = {
    "reject": {
        "sandbox_approval": True,
        "rules": True,
        "mcp_elicitations": True,
    }
}
DEFAULT_THREAD_SANDBOX = "workspace-write"


@dataclass(slots=True)
class TrackerConfig:
    kind: Optional[str] = None
    endpoint: Optional[str] = DEFAULT_LINEAR_ENDPOINT
    api_key: Optional[str] = None
    project_slug: Optional[str] = None
    project_key: Optional[str] = None
    email: Optional[str] = None
    assignee: Optional[str] = None
    active_states: list[str] = field(default_factory=lambda: list(DEFAULT_ACTIVE_STATES))
    terminal_states: list[str] = field(default_factory=lambda: list(DEFAULT_TERMINAL_STATES))
    memory_file: Optional[Path] = None


@dataclass(slots=True)
class HookConfig:
    after_create: Optional[str] = None
    before_run: Optional[str] = None
    after_run: Optional[str] = None
    before_remove: Optional[str] = None
    timeout_ms: int = DEFAULT_HOOK_TIMEOUT_MS


@dataclass(slots=True)
class AgentConfig:
    max_concurrent_agents: int = DEFAULT_MAX_CONCURRENT_AGENTS
    max_turns: int = DEFAULT_MAX_TURNS
    max_retry_backoff_ms: int = DEFAULT_MAX_RETRY_BACKOFF_MS
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class CodexConfig:
    command: str = DEFAULT_CODEX_COMMAND
    approval_policy: str | dict[str, object] = field(
        default_factory=lambda: DEFAULT_APPROVAL_POLICY.copy()
        if isinstance(DEFAULT_APPROVAL_POLICY, dict)
        else DEFAULT_APPROVAL_POLICY
    )
    thread_sandbox: str = DEFAULT_THREAD_SANDBOX
    turn_sandbox_policy: Optional[dict[str, object]] = None
    turn_timeout_ms: int = DEFAULT_CODEX_TURN_TIMEOUT_MS
    read_timeout_ms: int = DEFAULT_CODEX_READ_TIMEOUT_MS
    stall_timeout_ms: int = DEFAULT_CODEX_STALL_TIMEOUT_MS


@dataclass(slots=True)
class ServerConfig:
    host: str = DEFAULT_SERVER_HOST
    port: Optional[int] = None


@dataclass(slots=True)
class ServiceConfig:
    workflow_path: Path
    prompt_template: str
    tracker: TrackerConfig
    poll_interval_ms: int
    workspace_root: Path
    hooks: HookConfig
    agent: AgentConfig
    codex: CodexConfig
    server: ServerConfig

    @classmethod
    def from_workflow(cls, workflow: WorkflowDefinition) -> "ServiceConfig":
        raw = _normalize_keys(workflow.config)

        tracker = raw.get("tracker", {}) if isinstance(raw.get("tracker"), dict) else {}
        polling = raw.get("polling", {}) if isinstance(raw.get("polling"), dict) else {}
        workspace = raw.get("workspace", {}) if isinstance(raw.get("workspace"), dict) else {}
        hooks = raw.get("hooks", {}) if isinstance(raw.get("hooks"), dict) else {}
        agent = raw.get("agent", {}) if isinstance(raw.get("agent"), dict) else {}
        codex = raw.get("codex", {}) if isinstance(raw.get("codex"), dict) else {}
        server = raw.get("server", {}) if isinstance(raw.get("server"), dict) else {}

        tracker_kind = _normalize_tracker_kind(_string(tracker.get("kind")))
        tracker_endpoint_default = DEFAULT_LINEAR_ENDPOINT if tracker_kind in {None, "linear"} else DEFAULT_JIRA_ENDPOINT
        tracker_env_api_key_fallback = os.getenv("LINEAR_API_KEY") if tracker_kind in {None, "linear"} else os.getenv("JIRA_API_TOKEN")
        tracker_env_assignee_fallback = os.getenv("LINEAR_ASSIGNEE") if tracker_kind in {None, "linear"} else os.getenv("JIRA_ASSIGNEE")

        tracker_cfg = TrackerConfig(
            kind=tracker_kind,
            endpoint=_normalize_secret(
                _resolve_env_scalar(
                    _string_keep_empty(tracker.get("endpoint")),
                    tracker_endpoint_default,
                )
            ),
            api_key=_normalize_secret(
                _resolve_env_scalar(
                    _string_keep_empty(tracker.get("api_key")),
                    tracker_env_api_key_fallback,
                )
            ),
            project_slug=_normalize_secret(_string(tracker.get("project_slug"))),
            project_key=_normalize_secret(
                _resolve_env_scalar(
                    _string_keep_empty(tracker.get("project_key")),
                    os.getenv("JIRA_PROJECT_KEY"),
                )
            ),
            email=_normalize_secret(
                _resolve_env_scalar(
                    _string_keep_empty(tracker.get("email")),
                    os.getenv("JIRA_EMAIL"),
                )
            ),
            assignee=_normalize_secret(
                _resolve_env_scalar(_string_keep_empty(tracker.get("assignee")), tracker_env_assignee_fallback)
            ),
            active_states=_csv_list(tracker.get("active_states")) or list(DEFAULT_ACTIVE_STATES),
            terminal_states=_csv_list(tracker.get("terminal_states")) or list(DEFAULT_TERMINAL_STATES),
            memory_file=_optional_path(_string(tracker.get("memory_file"))),
        )

        hook_timeout = _positive_int(hooks.get("timeout_ms"), DEFAULT_HOOK_TIMEOUT_MS)
        hooks_cfg = HookConfig(
            after_create=_script(hooks.get("after_create")),
            before_run=_script(hooks.get("before_run")),
            after_run=_script(hooks.get("after_run")),
            before_remove=_script(hooks.get("before_remove")),
            timeout_ms=hook_timeout,
        )

        by_state_limits = {}
        raw_by_state = agent.get("max_concurrent_agents_by_state")
        if isinstance(raw_by_state, dict):
            for state_name, value in raw_by_state.items():
                parsed = _positive_int(value, None)
                if parsed is not None:
                    by_state_limits[_normalize_state(str(state_name))] = parsed

        agent_cfg = AgentConfig(
            max_concurrent_agents=_positive_int(agent.get("max_concurrent_agents"), DEFAULT_MAX_CONCURRENT_AGENTS),
            max_turns=_positive_int(agent.get("max_turns"), DEFAULT_MAX_TURNS),
            max_retry_backoff_ms=_positive_int(agent.get("max_retry_backoff_ms"), DEFAULT_MAX_RETRY_BACKOFF_MS),
            max_concurrent_agents_by_state=by_state_limits,
        )

        explicit_turn_sandbox = codex.get("turn_sandbox_policy")
        turn_sandbox = explicit_turn_sandbox if isinstance(explicit_turn_sandbox, dict) else None

        codex_cfg = CodexConfig(
            command=_string(codex.get("command")) or DEFAULT_CODEX_COMMAND,
            approval_policy=_approval_policy(codex.get("approval_policy")),
            thread_sandbox=_string(codex.get("thread_sandbox")) or DEFAULT_THREAD_SANDBOX,
            turn_sandbox_policy=turn_sandbox,
            turn_timeout_ms=_positive_int(codex.get("turn_timeout_ms"), DEFAULT_CODEX_TURN_TIMEOUT_MS),
            read_timeout_ms=_positive_int(codex.get("read_timeout_ms"), DEFAULT_CODEX_READ_TIMEOUT_MS),
            stall_timeout_ms=_non_negative_int(codex.get("stall_timeout_ms"), DEFAULT_CODEX_STALL_TIMEOUT_MS),
        )

        server_cfg = ServerConfig(
            host=_string(server.get("host")) or DEFAULT_SERVER_HOST,
            port=_non_negative_int(server.get("port"), None),
        )

        poll_interval = _positive_int(polling.get("interval_ms"), DEFAULT_POLL_INTERVAL_MS)
        workspace_root = _resolve_path(
            _string_keep_empty(workspace.get("root")),
            default=DEFAULT_WORKSPACE_ROOT,
        )
        prompt_template = workflow.prompt_template.strip() or DEFAULT_PROMPT_TEMPLATE

        return cls(
            workflow_path=workflow.path,
            prompt_template=prompt_template,
            tracker=tracker_cfg,
            poll_interval_ms=poll_interval,
            workspace_root=workspace_root,
            hooks=hooks_cfg,
            agent=agent_cfg,
            codex=codex_cfg,
            server=server_cfg,
        )

    def validate_dispatch(self) -> None:
        if not self.tracker.kind:
            raise ConfigValidationError("missing_tracker_kind")
        if self.tracker.kind not in {"linear", "jira", "memory"}:
            raise ConfigValidationError(f"unsupported_tracker_kind: {self.tracker.kind}")

        if self.tracker.kind == "linear":
            if not self.tracker.api_key:
                raise ConfigValidationError("missing_linear_api_token")
            if not self.tracker.project_slug:
                raise ConfigValidationError("missing_linear_project_slug")
            if not self.tracker.endpoint:
                raise ConfigValidationError("missing_linear_endpoint")

        if self.tracker.kind == "jira":
            if not self.tracker.api_key:
                raise ConfigValidationError("missing_jira_api_token")
            if not self.tracker.email:
                raise ConfigValidationError("missing_jira_email")
            if not self.tracker.project_key:
                raise ConfigValidationError("missing_jira_project_key")
            if not self.tracker.endpoint:
                raise ConfigValidationError("missing_jira_endpoint")

        if self.tracker.kind == "memory" and self.tracker.memory_file is None:
            raise ConfigValidationError("missing_memory_file")

        if not self.codex.command.strip():
            raise ConfigValidationError("missing_codex_command")

    @property
    def active_states(self) -> set[str]:
        return {_normalize_state(state) for state in self.tracker.active_states if state.strip()}

    @property
    def terminal_states(self) -> set[str]:
        return {_normalize_state(state) for state in self.tracker.terminal_states if state.strip()}

    def max_concurrent_for_state(self, state: str) -> int:
        key = _normalize_state(state)
        return self.agent.max_concurrent_agents_by_state.get(key, self.agent.max_concurrent_agents)

    def codex_turn_sandbox_policy(self, workspace: Path) -> dict[str, object]:
        if self.codex.turn_sandbox_policy:
            return self.codex.turn_sandbox_policy
        writable_root = str(workspace.expanduser().resolve())
        return {
            "type": "workspaceWrite",
            "writableRoots": [writable_root],
            "readOnlyAccess": {"type": "fullAccess"},
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }


def _normalize_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_keys(v) for v in value]
    return value


def _string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _string_keep_empty(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _csv_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            parsed = _string(item)
            if parsed:
                result.append(parsed)
        return result
    return []


def _to_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _positive_int(value: Any, default: Optional[int]) -> Optional[int]:
    parsed = _to_int(value)
    if parsed is None or parsed <= 0:
        return default
    return parsed


def _non_negative_int(value: Any, default: Optional[int]) -> Optional[int]:
    parsed = _to_int(value)
    if parsed is None or parsed < 0:
        return default
    return parsed


def _resolve_env_scalar(value: Optional[str], fallback: Optional[str]) -> Optional[str]:
    if value is None:
        return fallback
    ref = _env_reference(value)
    if ref:
        env_value = os.getenv(ref)
        if env_value is None:
            return fallback
        if env_value.strip() == "":
            return None
        return env_value.strip()
    return value


def _env_reference(value: str) -> Optional[str]:
    if value.startswith("$"):
        name = value[1:]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return name
    return None


def _resolve_path(value: Optional[str], default: Path) -> Path:
    if value is None:
        return default

    candidate = value.strip()
    if candidate == "":
        return default

    ref = _env_reference(candidate)
    if ref:
        resolved = os.getenv(ref)
        if not resolved:
            return default
        candidate = resolved

    candidate = os.path.expanduser(candidate)
    path = Path(candidate)

    if any(token in candidate for token in ("/", "\\")) or candidate.startswith("."):
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    return path


def _script(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    trimmed = value.rstrip()
    return trimmed or None


def _normalize_secret(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _normalize_state(state: str) -> str:
    return state.strip().lower()


def _normalize_tracker_kind(kind: Optional[str]) -> Optional[str]:
    if kind is None:
        return None
    normalized = kind.strip().lower()
    return normalized or None


def _approval_policy(value: Any) -> str | dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed
    if isinstance(DEFAULT_APPROVAL_POLICY, dict):
        return DEFAULT_APPROVAL_POLICY.copy()
    return DEFAULT_APPROVAL_POLICY


def _optional_path(value: Optional[str]) -> Optional[Path]:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    return Path(os.path.expanduser(candidate))
