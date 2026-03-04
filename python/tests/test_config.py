import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from symphony_service.config import ServiceConfig
from symphony_service.errors import ConfigValidationError
from symphony_service.models import WorkflowDefinition


class ConfigTests(unittest.TestCase):
    def _workflow(self, config: dict, prompt: str = "Prompt") -> WorkflowDefinition:
        return WorkflowDefinition(
            path=Path("WORKFLOW.md"),
            config=config,
            prompt_template=prompt,
            loaded_at=datetime.now(timezone.utc),
            mtime_ns=1,
        )

    def test_env_resolution_for_linear_token(self) -> None:
        old = os.environ.get("LINEAR_API_KEY")
        os.environ["LINEAR_API_KEY"] = "secret-token"
        try:
            workflow = self._workflow(
                {
                    "tracker": {
                        "kind": "linear",
                        "api_key": "$LINEAR_API_KEY",
                        "project_slug": "demo",
                    }
                }
            )
            config = ServiceConfig.from_workflow(workflow)
            self.assertEqual("secret-token", config.tracker.api_key)
            config.validate_dispatch()
        finally:
            if old is None:
                os.environ.pop("LINEAR_API_KEY", None)
            else:
                os.environ["LINEAR_API_KEY"] = old

    def test_env_resolution_for_jira_credentials(self) -> None:
        old_token = os.environ.get("JIRA_API_TOKEN")
        old_email = os.environ.get("JIRA_EMAIL")
        old_project = os.environ.get("JIRA_PROJECT_KEY")
        old_endpoint = os.environ.get("JIRA_ENDPOINT")
        os.environ["JIRA_API_TOKEN"] = "jira-token"
        os.environ["JIRA_EMAIL"] = "dev@example.com"
        os.environ["JIRA_PROJECT_KEY"] = "ENG"
        os.environ["JIRA_ENDPOINT"] = "https://example.atlassian.net"
        try:
            workflow = self._workflow(
                {
                    "tracker": {
                        "kind": "jira",
                        "api_key": "$JIRA_API_TOKEN",
                        "email": "$JIRA_EMAIL",
                        "project_key": "$JIRA_PROJECT_KEY",
                        "endpoint": "$JIRA_ENDPOINT",
                    }
                }
            )
            config = ServiceConfig.from_workflow(workflow)
            self.assertEqual("jira-token", config.tracker.api_key)
            self.assertEqual("dev@example.com", config.tracker.email)
            self.assertEqual("ENG", config.tracker.project_key)
            self.assertEqual("https://example.atlassian.net", config.tracker.endpoint)
            config.validate_dispatch()
        finally:
            if old_token is None:
                os.environ.pop("JIRA_API_TOKEN", None)
            else:
                os.environ["JIRA_API_TOKEN"] = old_token
            if old_email is None:
                os.environ.pop("JIRA_EMAIL", None)
            else:
                os.environ["JIRA_EMAIL"] = old_email
            if old_project is None:
                os.environ.pop("JIRA_PROJECT_KEY", None)
            else:
                os.environ["JIRA_PROJECT_KEY"] = old_project
            if old_endpoint is None:
                os.environ.pop("JIRA_ENDPOINT", None)
            else:
                os.environ["JIRA_ENDPOINT"] = old_endpoint

    def test_missing_tracker_kind_fails_validation(self) -> None:
        config = ServiceConfig.from_workflow(self._workflow({}))
        with self.assertRaises(ConfigValidationError):
            config.validate_dispatch()

    def test_memory_tracker_requires_memory_file(self) -> None:
        config = ServiceConfig.from_workflow(self._workflow({"tracker": {"kind": "memory"}}))
        with self.assertRaises(ConfigValidationError):
            config.validate_dispatch()

        with tempfile.TemporaryDirectory() as tmp:
            memory_file = Path(tmp) / "issues.json"
            memory_file.write_text("[]", encoding="utf-8")
            config = ServiceConfig.from_workflow(
                self._workflow({"tracker": {"kind": "memory", "memory_file": str(memory_file)}})
            )
            config.validate_dispatch()

    def test_jira_tracker_requires_required_fields(self) -> None:
        config = ServiceConfig.from_workflow(
            self._workflow(
                {
                    "tracker": {
                        "kind": "jira",
                        "endpoint": "https://example.atlassian.net",
                    }
                }
            )
        )
        with self.assertRaises(ConfigValidationError):
            config.validate_dispatch()

        config = ServiceConfig.from_workflow(
            self._workflow(
                {
                    "tracker": {
                        "kind": "jira",
                        "endpoint": "https://example.atlassian.net",
                        "api_key": "token",
                        "email": "dev@example.com",
                        "project_key": "ENG",
                    }
                }
            )
        )
        config.validate_dispatch()


if __name__ == "__main__":
    unittest.main()
