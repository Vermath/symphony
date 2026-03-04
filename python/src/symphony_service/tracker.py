"""Issue tracker adapters."""

from __future__ import annotations

import base64
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from .config import ServiceConfig
from .errors import TrackerError
from .models import Issue

LOGGER = logging.getLogger(__name__)
LINEAR_PAGE_SIZE = 50
JIRA_PAGE_SIZE = 50
JIRA_FIELDS = [
    "summary",
    "description",
    "priority",
    "status",
    "labels",
    "assignee",
    "issuelinks",
    "created",
    "updated",
]

LINEAR_QUERY_BY_STATES = """
query SymphonyLinearPoll(
  $projectSlug: String!,
  $stateNames: [String!]!,
  $first: Int!,
  $relationFirst: Int!,
  $after: String
) {
  issues(
    filter: {project: {slugId: {eq: $projectSlug}}, state: {name: {in: $stateNames}}},
    first: $first,
    after: $after
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      assignee { id }
      labels { nodes { name } }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue { id identifier state { name } }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

LINEAR_QUERY_BY_IDS = """
query SymphonyLinearIssuesById($ids: [ID!]!, $first: Int!, $relationFirst: Int!) {
  issues(filter: {id: {in: $ids}}, first: $first) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      assignee { id }
      labels { nodes { name } }
      inverseRelations(first: $relationFirst) {
        nodes {
          type
          issue { id identifier state { name } }
        }
      }
      createdAt
      updatedAt
    }
  }
}
"""

LINEAR_VIEWER_QUERY = """
query SymphonyLinearViewer {
  viewer { id }
}
"""


class TrackerAdapter(ABC):
    @abstractmethod
    def fetch_candidate_issues(self) -> list[Issue]:
        raise NotImplementedError

    @abstractmethod
    def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        raise NotImplementedError

    @abstractmethod
    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        raise NotImplementedError

    def graphql_raw(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        raise TrackerError("graphql_raw_not_supported")


class LinearTracker(TrackerAdapter):
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config
        self._session = requests.Session()
        self._assignee_filter: Optional[set[str]] = None

    def fetch_candidate_issues(self) -> list[Issue]:
        return self.fetch_issues_by_states(self._config.tracker.active_states)

    def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        if not states:
            return []

        assignee_filter = self._resolve_assignee_filter()
        cursor: Optional[str] = None
        issues: list[Issue] = []

        while True:
            payload = self._graphql(
                LINEAR_QUERY_BY_STATES,
                {
                    "projectSlug": self._config.tracker.project_slug,
                    "stateNames": list(dict.fromkeys(states)),
                    "first": LINEAR_PAGE_SIZE,
                    "relationFirst": LINEAR_PAGE_SIZE,
                    "after": cursor,
                },
            )

            nodes = _path(payload, "data", "issues", "nodes")
            page_info = _path(payload, "data", "issues", "pageInfo")
            if not isinstance(nodes, list):
                raise TrackerError("linear_unknown_payload")

            for node in nodes:
                issue = _normalize_linear_issue(node, assignee_filter)
                if issue is not None:
                    issues.append(issue)

            if not isinstance(page_info, dict):
                break
            has_next = bool(page_info.get("hasNextPage"))
            next_cursor = page_info.get("endCursor")
            if has_next and isinstance(next_cursor, str) and next_cursor:
                cursor = next_cursor
                continue
            if has_next and not next_cursor:
                raise TrackerError("linear_missing_end_cursor")
            break

        return issues

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        unique_ids = list(dict.fromkeys(issue_ids))
        if not unique_ids:
            return []

        assignee_filter = self._resolve_assignee_filter()
        payload = self._graphql(
            LINEAR_QUERY_BY_IDS,
            {
                "ids": unique_ids,
                "first": min(LINEAR_PAGE_SIZE, len(unique_ids)),
                "relationFirst": LINEAR_PAGE_SIZE,
            },
        )

        nodes = _path(payload, "data", "issues", "nodes")
        if not isinstance(nodes, list):
            raise TrackerError("linear_unknown_payload")

        issues: list[Issue] = []
        for node in nodes:
            issue = _normalize_linear_issue(node, assignee_filter)
            if issue is not None:
                issues.append(issue)
        return issues

    def graphql_raw(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._graphql(query, variables or {})

    def _resolve_assignee_filter(self) -> Optional[set[str]]:
        configured = self._config.tracker.assignee
        if not configured:
            return None
        if self._assignee_filter is not None:
            return self._assignee_filter

        normalized = configured.strip()
        if not normalized:
            return None
        if normalized.lower() != "me":
            self._assignee_filter = {normalized}
            return self._assignee_filter

        viewer_payload = self._graphql(LINEAR_VIEWER_QUERY, {})
        viewer_id = _path(viewer_payload, "data", "viewer", "id")
        if not isinstance(viewer_id, str) or not viewer_id.strip():
            raise TrackerError("missing_linear_viewer_identity")
        self._assignee_filter = {viewer_id.strip()}
        return self._assignee_filter

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        token = self._config.tracker.api_key
        endpoint = self._config.tracker.endpoint
        if not token:
            raise TrackerError("missing_linear_api_token")
        if not endpoint:
            raise TrackerError("missing_linear_endpoint")

        response = self._session.post(
            endpoint,
            headers={"Authorization": token, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
            timeout=30,
        )
        if response.status_code != 200:
            text = (response.text or "").strip().replace("\n", " ")
            truncated = text[:300] + ("..." if len(text) > 300 else "")
            LOGGER.error(
                "Linear GraphQL status=%s body=%s",
                response.status_code,
                truncated,
                extra={"component": "tracker"},
            )
            raise TrackerError(f"linear_api_status:{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TrackerError("linear_non_json_response") from exc
        if isinstance(payload, dict) and payload.get("errors"):
            raise TrackerError(f"linear_graphql_errors:{payload['errors']}")
        return payload


class JiraTracker(TrackerAdapter):
    """Jira Cloud REST-backed tracker adapter."""

    def __init__(self, config: ServiceConfig, session: Optional[requests.Session] = None) -> None:
        self._config = config
        self._session = session or requests.Session()

    def fetch_candidate_issues(self) -> list[Issue]:
        return self.fetch_issues_by_states(self._config.tracker.active_states)

    def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        if not states:
            return []

        jql = self._jql_for_states(states)
        nodes = self._search_issues(jql)
        return [issue for issue in (_normalize_jira_issue(node, self._config) for node in nodes) if issue is not None]

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        unique_ids = [issue_id for issue_id in dict.fromkeys(issue_ids) if isinstance(issue_id, str) and issue_id.strip()]
        if not unique_ids:
            return []
        jql_ids = ", ".join(_jql_quote(issue_id.strip()) for issue_id in unique_ids)
        jql = f"id in ({jql_ids})"
        nodes = self._search_issues(jql)
        return [issue for issue in (_normalize_jira_issue(node, self._config) for node in nodes) if issue is not None]

    def _jql_for_states(self, states: list[str]) -> str:
        project_key = self._config.tracker.project_key or ""
        status_values = ", ".join(_jql_quote(state) for state in states)
        clauses = [f"project = {_jql_quote(project_key)}", f"status in ({status_values})"]

        assignee = self._config.tracker.assignee
        if assignee:
            if assignee.strip().lower() == "me":
                clauses.append("assignee = currentUser()")
            else:
                clauses.append(f"assignee = {_jql_quote(assignee)}")

        return " AND ".join(clauses) + " ORDER BY priority ASC, created ASC"

    def _search_issues(self, jql: str) -> list[dict[str, Any]]:
        start_at = 0
        next_page_token: Optional[str] = None
        pages = 0
        issues: list[dict[str, Any]] = []

        while pages < 500:
            pages += 1
            body: dict[str, Any] = {
                "jql": jql,
                "fields": list(JIRA_FIELDS),
                "maxResults": JIRA_PAGE_SIZE,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            else:
                body["startAt"] = start_at

            payload = self._post_search(body)
            page_issues = payload.get("issues")
            if not isinstance(page_issues, list):
                raise TrackerError("jira_unknown_payload")

            issues.extend(page_issues)

            token = payload.get("nextPageToken")
            if isinstance(token, str) and token.strip():
                next_page_token = token.strip()
                continue

            is_last = payload.get("isLast")
            if isinstance(is_last, bool):
                if is_last:
                    break

            total = payload.get("total")
            current_start = payload.get("startAt", start_at)
            max_results = payload.get("maxResults", JIRA_PAGE_SIZE)
            if isinstance(total, int) and isinstance(current_start, int) and isinstance(max_results, int):
                next_start = current_start + max_results
                if next_start < total:
                    start_at = next_start
                    next_page_token = None
                    continue
                break

            if len(page_issues) < JIRA_PAGE_SIZE:
                break
            start_at += JIRA_PAGE_SIZE

        return issues

    def _post_search(self, body: dict[str, Any]) -> dict[str, Any]:
        endpoint = self._config.tracker.endpoint
        email = self._config.tracker.email
        api_key = self._config.tracker.api_key
        if not endpoint:
            raise TrackerError("missing_jira_endpoint")
        if not email:
            raise TrackerError("missing_jira_email")
        if not api_key:
            raise TrackerError("missing_jira_api_token")

        headers = self._jira_headers(email=email, api_key=api_key)
        response = self._session.post(
            self._jira_api_url("search/jql"),
            headers=headers,
            json=body,
            timeout=30,
        )
        if response.status_code == 404:
            response = self._session.post(
                self._jira_api_url("search"),
                headers=headers,
                json=body,
                timeout=30,
            )

        if response.status_code != 200:
            text = (response.text or "").strip().replace("\n", " ")
            truncated = text[:300] + ("..." if len(text) > 300 else "")
            LOGGER.error(
                "Jira search status=%s body=%s",
                response.status_code,
                truncated,
                extra={"component": "tracker"},
            )
            raise TrackerError(f"jira_api_status:{response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TrackerError("jira_non_json_response") from exc
        if isinstance(payload, dict) and isinstance(payload.get("errorMessages"), list) and payload.get("errorMessages"):
            raise TrackerError(f"jira_errors:{payload['errorMessages']}")
        return payload

    def _jira_headers(self, email: str, api_key: str) -> dict[str, str]:
        encoded = base64.b64encode(f"{email}:{api_key}".encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _jira_api_url(self, suffix: str) -> str:
        endpoint = (self._config.tracker.endpoint or "").strip().rstrip("/")
        if "/rest/api/" in endpoint:
            return f"{endpoint}/{suffix.lstrip('/')}"
        return f"{endpoint}/rest/api/3/{suffix.lstrip('/')}"


class MemoryTracker(TrackerAdapter):
    """Simple local tracker for offline/testing runs."""

    def __init__(self, config: ServiceConfig) -> None:
        if config.tracker.memory_file is None:
            raise TrackerError("missing_memory_file")
        self._config = config
        self._path = config.tracker.memory_file

    def fetch_candidate_issues(self) -> list[Issue]:
        return self.fetch_issues_by_states(self._config.tracker.active_states)

    def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        normalized = {state.strip().lower() for state in states}
        all_issues = self._load_issues()
        return [issue for issue in all_issues if issue.state.strip().lower() in normalized]

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        desired = set(issue_ids)
        return [issue for issue in self._load_issues() if issue.id in desired]

    def _load_issues(self) -> list[Issue]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TrackerError(f"memory_tracker_file_missing:{self._path}") from exc
        except json.JSONDecodeError as exc:
            raise TrackerError(f"memory_tracker_invalid_json:{self._path}") from exc

        if not isinstance(payload, list):
            raise TrackerError("memory_tracker_payload_must_be_list")

        issues: list[Issue] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("identifier") or item.get("id") or "").strip()
            issue_id = str(item.get("id") or identifier).strip()
            title = str(item.get("title") or "").strip()
            state = str(item.get("state") or "").strip()
            if not (issue_id and identifier and title and state):
                continue
            issues.append(
                Issue(
                    id=issue_id,
                    identifier=identifier,
                    title=title,
                    description=item.get("description"),
                    priority=item.get("priority") if isinstance(item.get("priority"), int) else None,
                    state=state,
                    branch_name=item.get("branch_name"),
                    url=item.get("url"),
                    labels=[str(x).lower() for x in item.get("labels", []) if isinstance(x, str)],
                    blocked_by=[blocker for blocker in item.get("blocked_by", []) if isinstance(blocker, dict)],
                    created_at=_parse_datetime(item.get("created_at")),
                    updated_at=_parse_datetime(item.get("updated_at")),
                    assignee_id=item.get("assignee_id"),
                    assigned_to_worker=bool(item.get("assigned_to_worker", True)),
                )
            )
        return issues


def build_tracker(config: ServiceConfig) -> TrackerAdapter:
    if config.tracker.kind == "memory":
        return MemoryTracker(config)
    if config.tracker.kind == "jira":
        return JiraTracker(config)
    return LinearTracker(config)


def _normalize_linear_issue(node: dict[str, Any], assignee_filter: Optional[set[str]]) -> Optional[Issue]:
    issue_id = node.get("id")
    identifier = node.get("identifier")
    title = node.get("title")
    state_name = _path(node, "state", "name")
    if not all(isinstance(value, str) and value.strip() for value in [issue_id, identifier, title, state_name]):
        return None

    assignee = node.get("assignee") if isinstance(node.get("assignee"), dict) else None
    assignee_id = assignee.get("id") if isinstance(assignee, dict) else None
    assigned_to_worker = True
    if assignee_filter is not None:
        assigned_to_worker = isinstance(assignee_id, str) and assignee_id in assignee_filter

    labels = []
    label_nodes = _path(node, "labels", "nodes")
    if isinstance(label_nodes, list):
        for entry in label_nodes:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                labels.append(entry["name"].strip().lower())

    blocked_by: list[dict[str, Optional[str]]] = []
    inverse_nodes = _path(node, "inverseRelations", "nodes")
    if isinstance(inverse_nodes, list):
        for relation in inverse_nodes:
            if not isinstance(relation, dict):
                continue
            relation_type = relation.get("type")
            related_issue = relation.get("issue")
            if not isinstance(relation_type, str) or relation_type.strip().lower() != "blocks":
                continue
            if not isinstance(related_issue, dict):
                continue
            blocked_by.append(
                {
                    "id": related_issue.get("id") if isinstance(related_issue.get("id"), str) else None,
                    "identifier": related_issue.get("identifier")
                    if isinstance(related_issue.get("identifier"), str)
                    else None,
                    "state": _path(related_issue, "state", "name")
                    if isinstance(_path(related_issue, "state", "name"), str)
                    else None,
                }
            )

    priority = node.get("priority") if isinstance(node.get("priority"), int) else None

    return Issue(
        id=issue_id.strip(),
        identifier=identifier.strip(),
        title=title.strip(),
        description=node.get("description") if isinstance(node.get("description"), str) else None,
        priority=priority,
        state=state_name.strip(),
        branch_name=node.get("branchName") if isinstance(node.get("branchName"), str) else None,
        url=node.get("url") if isinstance(node.get("url"), str) else None,
        labels=labels,
        blocked_by=blocked_by,
        created_at=_parse_datetime(node.get("createdAt")),
        updated_at=_parse_datetime(node.get("updatedAt")),
        assignee_id=assignee_id if isinstance(assignee_id, str) else None,
        assigned_to_worker=assigned_to_worker,
    )


def _normalize_jira_issue(node: dict[str, Any], config: ServiceConfig) -> Optional[Issue]:
    issue_id = node.get("id")
    identifier = node.get("key")
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    title = fields.get("summary")
    state_name = _path(fields, "status", "name")
    if not all(isinstance(value, str) and value.strip() for value in [issue_id, identifier, title, state_name]):
        return None

    assignee = fields.get("assignee") if isinstance(fields.get("assignee"), dict) else None
    assignee_id = assignee.get("accountId") if isinstance(assignee, dict) else None
    assigned_to_worker = _jira_assignee_matches(config, assignee)

    labels = []
    if isinstance(fields.get("labels"), list):
        labels = [str(label).strip().lower() for label in fields["labels"] if str(label).strip()]

    blocked_by: list[dict[str, Optional[str]]] = []
    links = fields.get("issuelinks")
    if isinstance(links, list):
        blocked_by = _jira_blockers_from_links(links)

    browse_root = (config.tracker.endpoint or "").rstrip("/")
    if "/rest/api/" in browse_root:
        browse_root = browse_root.split("/rest/api/")[0].rstrip("/")
    url = f"{browse_root}/browse/{identifier.strip()}" if browse_root else None

    priority = _jira_priority(fields.get("priority"))
    description = _jira_description(fields.get("description"))

    return Issue(
        id=issue_id.strip(),
        identifier=identifier.strip(),
        title=title.strip(),
        description=description,
        priority=priority,
        state=state_name.strip(),
        branch_name=None,
        url=url,
        labels=labels,
        blocked_by=blocked_by,
        created_at=_parse_datetime(fields.get("created")),
        updated_at=_parse_datetime(fields.get("updated")),
        assignee_id=assignee_id if isinstance(assignee_id, str) else None,
        assigned_to_worker=assigned_to_worker,
    )


def _jira_priority(raw_priority: Any) -> Optional[int]:
    if isinstance(raw_priority, dict):
        raw_id = raw_priority.get("id")
        if isinstance(raw_id, str) and raw_id.strip().isdigit():
            parsed = int(raw_id.strip())
            return parsed if parsed >= 0 else None
        rank = raw_priority.get("priority")
        if isinstance(rank, int):
            return rank
    return None


def _jira_assignee_matches(config: ServiceConfig, assignee: Optional[dict[str, Any]]) -> bool:
    configured = config.tracker.assignee
    if not configured:
        return True
    normalized = configured.strip().lower()
    if normalized == "me":
        return True

    account_id = assignee.get("accountId") if isinstance(assignee, dict) else None
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip() == configured.strip()
    return False


def _jira_blockers_from_links(links: list[dict[str, Any]]) -> list[dict[str, Optional[str]]]:
    blockers: list[dict[str, Optional[str]]] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") if isinstance(link.get("type"), dict) else {}
        inward_issue = link.get("inwardIssue") if isinstance(link.get("inwardIssue"), dict) else None
        outward_issue = link.get("outwardIssue") if isinstance(link.get("outwardIssue"), dict) else None

        if inward_issue and _jira_link_is_blocked_by(link_type, direction="inward"):
            blockers.append(_jira_linked_issue(inward_issue))
        if outward_issue and _jira_link_is_blocked_by(link_type, direction="outward"):
            blockers.append(_jira_linked_issue(outward_issue))
    return blockers


def _jira_link_is_blocked_by(link_type: dict[str, Any], direction: str) -> bool:
    name = str(link_type.get("name") or "").strip().lower()
    inward = str(link_type.get("inward") or "").strip().lower()
    outward = str(link_type.get("outward") or "").strip().lower()

    if direction == "inward":
        return "blocked by" in inward or name == "blocks"
    return "blocked by" in outward


def _jira_linked_issue(linked_issue: dict[str, Any]) -> dict[str, Optional[str]]:
    issue_id = linked_issue.get("id") if isinstance(linked_issue.get("id"), str) else None
    identifier = linked_issue.get("key") if isinstance(linked_issue.get("key"), str) else None
    state = _path(linked_issue, "fields", "status", "name")
    if not isinstance(state, str):
        state = _path(linked_issue, "status", "name")
    return {
        "id": issue_id,
        "identifier": identifier,
        "state": state if isinstance(state, str) else None,
    }


def _jira_description(value: Any) -> Optional[str]:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed if trimmed else None
    if not isinstance(value, dict):
        return None

    chunks: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
            content = node.get("content")
            if isinstance(content, list):
                for item in content:
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    if not chunks:
        return None
    return "\n".join(chunks)


def _jql_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _path(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current

