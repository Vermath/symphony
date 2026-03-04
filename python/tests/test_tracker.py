import unittest
from datetime import datetime, timezone
from pathlib import Path

from symphony_service.config import ServiceConfig
from symphony_service.models import WorkflowDefinition
from symphony_service.tracker import JiraTracker, build_tracker


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError("No fake responses left")
        return self.responses.pop(0)


def _workflow(config: dict) -> WorkflowDefinition:
    return WorkflowDefinition(
        path=Path("WORKFLOW.md"),
        config=config,
        prompt_template="Prompt",
        loaded_at=datetime.now(timezone.utc),
        mtime_ns=1,
    )


class JiraTrackerTests(unittest.TestCase):
    def test_build_tracker_returns_jira_tracker(self) -> None:
        config = ServiceConfig.from_workflow(
            _workflow(
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
        tracker = build_tracker(config)
        self.assertIsInstance(tracker, JiraTracker)

    def test_fetch_candidate_issues_normalizes_jira_payload(self) -> None:
        payload = {
            "issues": [
                {
                    "id": "10001",
                    "key": "ENG-123",
                    "fields": {
                        "summary": "Fix login flow",
                        "description": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "First line"},
                                        {"type": "text", "text": "Second line"},
                                    ],
                                }
                            ],
                        },
                        "priority": {"id": "2", "name": "High"},
                        "status": {"name": "Todo"},
                        "labels": ["Backend", "Auth"],
                        "assignee": {"accountId": "acct-1"},
                        "issuelinks": [
                            {
                                "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                                "inwardIssue": {
                                    "id": "10000",
                                    "key": "ENG-122",
                                    "fields": {"status": {"name": "In Progress"}},
                                },
                            }
                        ],
                        "created": "2026-03-04T10:00:00.000+0000",
                        "updated": "2026-03-04T11:00:00.000+0000",
                    },
                }
            ],
            "isLast": True,
        }
        session = _FakeSession([_FakeResponse(200, payload)])
        config = ServiceConfig.from_workflow(
            _workflow(
                {
                    "tracker": {
                        "kind": "jira",
                        "endpoint": "https://example.atlassian.net",
                        "api_key": "token",
                        "email": "dev@example.com",
                        "project_key": "ENG",
                        "active_states": ["Todo"],
                    }
                }
            )
        )
        tracker = JiraTracker(config, session=session)
        issues = tracker.fetch_candidate_issues()

        self.assertEqual(1, len(issues))
        issue = issues[0]
        self.assertEqual("10001", issue.id)
        self.assertEqual("ENG-123", issue.identifier)
        self.assertEqual("Fix login flow", issue.title)
        self.assertEqual("Todo", issue.state)
        self.assertEqual(["backend", "auth"], issue.labels)
        self.assertEqual(1, len(issue.blocked_by))
        self.assertEqual("ENG-122", issue.blocked_by[0]["identifier"])
        self.assertIn("First line", issue.description or "")
        self.assertIn("/browse/ENG-123", issue.url or "")

        jql = session.calls[0]["json"]["jql"]
        self.assertIn('project = "ENG"', jql)
        self.assertIn('status in ("Todo")', jql)

    def test_search_endpoint_fallback_from_search_jql(self) -> None:
        session = _FakeSession(
            [
                _FakeResponse(404, {"errorMessages": ["Not found"]}),
                _FakeResponse(200, {"issues": [], "isLast": True}),
            ]
        )
        config = ServiceConfig.from_workflow(
            _workflow(
                {
                    "tracker": {
                        "kind": "jira",
                        "endpoint": "https://example.atlassian.net",
                        "api_key": "token",
                        "email": "dev@example.com",
                        "project_key": "ENG",
                        "active_states": ["Todo"],
                    }
                }
            )
        )
        tracker = JiraTracker(config, session=session)
        issues = tracker.fetch_candidate_issues()
        self.assertEqual([], issues)
        self.assertTrue(session.calls[0]["url"].endswith("/rest/api/3/search/jql"))
        self.assertTrue(session.calls[1]["url"].endswith("/rest/api/3/search"))


if __name__ == "__main__":
    unittest.main()

