"""Workspace lifecycle and safety controls."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from shutil import which
from typing import Optional

from .config import ServiceConfig
from .errors import WorkspaceError
from .models import Issue

LOGGER = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config
        self._excluded_entries = {".elixir_ls", "tmp"}

    @property
    def root(self) -> Path:
        return self._config.workspace_root

    def workspace_path_for_issue(self, issue_identifier: str) -> Path:
        safe_id = self.safe_identifier(issue_identifier)
        return self.root / safe_id

    def create_for_issue(self, issue: Issue) -> tuple[Path, bool]:
        self.root.mkdir(parents=True, exist_ok=True)
        workspace = self.workspace_path_for_issue(issue.identifier)
        self._validate_workspace_path(workspace)

        created_now = False
        if workspace.is_dir():
            self._clean_tmp_artifacts(workspace)
        else:
            if workspace.exists():
                if workspace.is_file():
                    workspace.unlink()
                else:
                    shutil.rmtree(workspace, ignore_errors=True)
            workspace.mkdir(parents=True, exist_ok=True)
            created_now = True

        if created_now and self._config.hooks.after_create:
            self._run_hook(
                command=self._config.hooks.after_create,
                workspace=workspace,
                issue=issue,
                hook_name="after_create",
                ignore_failure=False,
            )

        return workspace, created_now

    def run_before_run_hook(self, workspace: Path, issue: Issue) -> None:
        command = self._config.hooks.before_run
        if not command:
            return
        self._run_hook(command, workspace, issue, "before_run", ignore_failure=False)

    def run_after_run_hook(self, workspace: Path, issue: Issue) -> None:
        command = self._config.hooks.after_run
        if not command:
            return
        self._run_hook(command, workspace, issue, "after_run", ignore_failure=True)

    def remove_issue_workspaces(self, issue_identifier: str) -> None:
        workspace = self.workspace_path_for_issue(issue_identifier)
        self.remove_workspace(workspace, issue_identifier)

    def remove_workspace(self, workspace: Path, issue_identifier: str) -> None:
        if not workspace.exists():
            return
        self._validate_workspace_path(workspace)
        command = self._config.hooks.before_remove
        if command:
            pseudo_issue = Issue(
                id=issue_identifier,
                identifier=issue_identifier,
                title=issue_identifier,
                description=None,
                priority=None,
                state="terminal",
            )
            self._run_hook(command, workspace, pseudo_issue, "before_remove", ignore_failure=True)
        shutil.rmtree(workspace, ignore_errors=True)

    @staticmethod
    def safe_identifier(identifier: str) -> str:
        if not identifier:
            return "issue"
        return re.sub(r"[^A-Za-z0-9._-]", "_", identifier)

    def _validate_workspace_path(self, workspace: Path) -> None:
        root = self.root.expanduser().resolve()
        target = workspace.expanduser().resolve()

        if target == root:
            raise WorkspaceError(f"workspace_equals_root:{target}")
        if root not in target.parents:
            raise WorkspaceError(f"workspace_outside_root:{target}:{root}")
        self._ensure_no_symlink_components(root, target)

    def _ensure_no_symlink_components(self, root: Path, target: Path) -> None:
        current = root
        relative_parts = target.relative_to(root).parts
        for part in relative_parts:
            current = current / part
            if not current.exists():
                return
            if current.is_symlink():
                raise WorkspaceError(f"workspace_symlink_escape:{current}:{root}")

    def _clean_tmp_artifacts(self, workspace: Path) -> None:
        for entry in self._excluded_entries:
            path = workspace / entry
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

    def _run_hook(
        self,
        command: str,
        workspace: Path,
        issue: Issue,
        hook_name: str,
        ignore_failure: bool,
    ) -> None:
        timeout_seconds = max(1, int(self._config.hooks.timeout_ms / 1000))
        shell_cmd = _shell_command(command)
        try:
            result = subprocess.run(
                shell_cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            LOGGER.warning(
                "Workspace hook timed out",
                extra={
                    "component": "workspace",
                    "issue_id": issue.id,
                    "issue_identifier": issue.identifier,
                    "event": hook_name,
                },
            )
            if not ignore_failure:
                raise WorkspaceError(f"workspace_hook_timeout:{hook_name}:{self._config.hooks.timeout_ms}") from exc
            return

        if result.returncode == 0:
            return

        output = (result.stdout or "") + (result.stderr or "")
        truncated = output[:2048] + ("... (truncated)" if len(output) > 2048 else "")
        LOGGER.warning(
            "Workspace hook failed status=%s output=%s",
            result.returncode,
            truncated.replace("\n", " "),
            extra={
                "component": "workspace",
                "issue_id": issue.id,
                "issue_identifier": issue.identifier,
                "event": hook_name,
            },
        )
        if not ignore_failure:
            raise WorkspaceError(f"workspace_hook_failed:{hook_name}:{result.returncode}")


def _shell_command(script: str) -> list[str]:
    if os.name == "nt":
        if which("powershell"):
            return ["powershell", "-Command", script]
        if which("cmd"):
            return ["cmd", "/c", script]
        if which("bash"):
            return ["bash", "-lc", script]
        if which("sh"):
            return ["sh", "-lc", script]
        return ["cmd", "/c", script]

    if which("bash"):
        return ["bash", "-lc", script]
    if which("sh"):
        return ["sh", "-lc", script]
    return ["cmd", "/c", script]
