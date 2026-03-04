"""Workflow file loading, parsing, and dynamic reload."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .errors import WorkflowError
from .models import WorkflowDefinition

DEFAULT_WORKFLOW_FILE = "WORKFLOW.md"


def _split_front_matter(content: str) -> tuple[str, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", content

    front: list[str] = []
    prompt_start = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            prompt_start = index + 1
            break
        front.append(line)

    prompt_lines = lines[prompt_start:] if prompt_start < len(lines) else []
    return "\n".join(front), "\n".join(prompt_lines)


def parse_workflow_text(path: Path, content: str, mtime_ns: int) -> WorkflowDefinition:
    front_matter_text, prompt_text = _split_front_matter(content)
    config: dict[str, object]

    if front_matter_text.strip() == "":
        config = {}
    else:
        try:
            decoded = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as exc:
            raise WorkflowError(f"workflow_parse_error: {exc}") from exc
        if decoded is None:
            config = {}
        elif not isinstance(decoded, dict):
            raise WorkflowError("workflow_front_matter_not_a_map")
        else:
            config = decoded

    return WorkflowDefinition(
        path=path,
        config=config,
        prompt_template=prompt_text.strip(),
        loaded_at=datetime.now(timezone.utc),
        mtime_ns=mtime_ns,
    )


def load_workflow(path: Path) -> WorkflowDefinition:
    try:
        resolved = path.expanduser()
        mtime_ns = resolved.stat().st_mtime_ns
        content = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkflowError(f"missing_workflow_file: {path}") from exc
    except OSError as exc:
        raise WorkflowError(f"missing_workflow_file: {path}: {exc}") from exc

    return parse_workflow_text(resolved, content, mtime_ns)


@dataclass(slots=True)
class WorkflowSnapshot:
    workflow: WorkflowDefinition
    changed: bool
    error: Optional[Exception] = None


class WorkflowStore:
    """Holds last-good workflow and supports hot reload by file mtime."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._current: Optional[WorkflowDefinition] = None
        self._last_good: Optional[WorkflowDefinition] = None
        self._last_error: Optional[Exception] = None

    @property
    def path(self) -> Path:
        return self._path

    def set_path(self, path: Path) -> None:
        with self._lock:
            self._path = path
            self._current = None
            self._last_good = None
            self._last_error = None

    def load_initial(self) -> WorkflowDefinition:
        with self._lock:
            loaded = load_workflow(self._path)
            self._current = loaded
            self._last_good = loaded
            self._last_error = None
            return loaded

    def current(self) -> WorkflowDefinition:
        with self._lock:
            if self._current is None:
                loaded = load_workflow(self._path)
                self._current = loaded
                self._last_good = loaded
                self._last_error = None
            return self._current

    def refresh(self) -> WorkflowSnapshot:
        with self._lock:
            if self._current is None:
                loaded = load_workflow(self._path)
                self._current = loaded
                self._last_good = loaded
                self._last_error = None
                return WorkflowSnapshot(workflow=loaded, changed=True, error=None)

            try:
                stat = self._path.expanduser().stat()
            except OSError as exc:
                error = WorkflowError(f"missing_workflow_file: {self._path}: {exc}")
                self._last_error = error
                if self._last_good is not None:
                    self._current = self._last_good
                    return WorkflowSnapshot(workflow=self._current, changed=False, error=error)
                raise error

            if stat.st_mtime_ns == self._current.mtime_ns:
                return WorkflowSnapshot(workflow=self._current, changed=False, error=None)

            try:
                loaded = load_workflow(self._path)
            except Exception as exc:
                self._last_error = exc
                if self._last_good is not None:
                    self._current = self._last_good
                    return WorkflowSnapshot(workflow=self._current, changed=False, error=exc)
                raise

            self._current = loaded
            self._last_good = loaded
            self._last_error = None
            return WorkflowSnapshot(workflow=loaded, changed=True, error=None)

