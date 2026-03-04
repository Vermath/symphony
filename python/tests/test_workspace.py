import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from symphony_service.config import AgentConfig, CodexConfig, HookConfig, ServerConfig, ServiceConfig, TrackerConfig
from symphony_service.errors import WorkspaceError
from symphony_service.models import Issue
from symphony_service.workspace import WorkspaceManager


def _config(workspace_root: Path) -> ServiceConfig:
    return ServiceConfig(
        workflow_path=Path("WORKFLOW.md"),
        prompt_template="Prompt",
        tracker=TrackerConfig(kind="memory", memory_file=workspace_root / "issues.json"),
        poll_interval_ms=30_000,
        workspace_root=workspace_root,
        hooks=HookConfig(),
        agent=AgentConfig(),
        codex=CodexConfig(),
        server=ServerConfig(),
    )


class WorkspaceTests(unittest.TestCase):
    def test_create_workspace_for_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkspaceManager(_config(Path(tmp)))
            issue = Issue(
                id="issue-1",
                identifier="ABC-1/unsafe",
                title="Test",
                description=None,
                priority=None,
                state="Todo",
                created_at=datetime.now(timezone.utc),
            )
            workspace, created = manager.create_for_issue(issue)
            self.assertTrue(created)
            self.assertTrue(workspace.exists())
            self.assertIn("ABC-1_unsafe", str(workspace))

    def test_before_run_hook_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(Path(tmp))
            cfg.hooks.before_run = "exit 1"
            manager = WorkspaceManager(cfg)
            issue = Issue(id="1", identifier="A-1", title="T", description=None, priority=None, state="Todo")
            workspace, _ = manager.create_for_issue(issue)
            with self.assertRaises(WorkspaceError):
                manager.run_before_run_hook(workspace, issue)

    def test_workspace_outside_root_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkspaceManager(_config(Path(tmp)))
            with self.assertRaises(WorkspaceError):
                manager._validate_workspace_path(Path(tmp).parent / "outside")  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()

