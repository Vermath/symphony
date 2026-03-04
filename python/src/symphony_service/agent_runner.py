"""Worker attempt execution for a single issue."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from .codex_app_server import CodexAppServerClient
from .config import ServiceConfig
from .errors import AppServerError, TemplateParseError, TemplateRenderError, TrackerError, WorkspaceError
from .models import Issue, WorkerResult
from .prompt import render_prompt
from .tracker import TrackerAdapter
from .workspace import WorkspaceManager

LOGGER = logging.getLogger(__name__)

UpdateCallback = Callable[[dict], None]


class AgentRunner:
    def __init__(
        self,
        config: ServiceConfig,
        tracker: TrackerAdapter,
        workspace_manager: WorkspaceManager,
        app_server: CodexAppServerClient,
    ) -> None:
        self._config = config
        self._tracker = tracker
        self._workspace_manager = workspace_manager
        self._app_server = app_server

    def run(
        self,
        issue: Issue,
        attempt: Optional[int],
        cancel_event: threading.Event,
        on_update: Optional[UpdateCallback] = None,
    ) -> WorkerResult:
        callback = on_update or (lambda _: None)
        started_at = datetime.now(timezone.utc)
        workspace, _ = self._workspace_manager.create_for_issue(issue)

        LOGGER.info(
            "Starting agent run",
            extra={"issue_id": issue.id, "issue_identifier": issue.identifier, "component": "runner"},
        )

        try:
            self._workspace_manager.run_before_run_hook(workspace, issue)
            session = self._app_server.start_session(workspace)
            try:
                self._run_turn_loop(session, issue, attempt, cancel_event, callback)
            finally:
                self._app_server.stop_session(session)
        except (WorkspaceError, AppServerError, TrackerError, TemplateParseError, TemplateRenderError) as exc:
            ended = datetime.now(timezone.utc)
            LOGGER.error(
                "Agent run failed: %s",
                exc,
                extra={"issue_id": issue.id, "issue_identifier": issue.identifier, "component": "runner", "error": str(exc)},
            )
            return WorkerResult(
                issue_id=issue.id,
                identifier=issue.identifier,
                started_at=started_at,
                ended_at=ended,
                success=False,
                reason=str(exc),
            )
        finally:
            self._workspace_manager.run_after_run_hook(workspace, issue)

        ended_at = datetime.now(timezone.utc)
        LOGGER.info(
            "Agent run completed",
            extra={"issue_id": issue.id, "issue_identifier": issue.identifier, "component": "runner"},
        )
        return WorkerResult(
            issue_id=issue.id,
            identifier=issue.identifier,
            started_at=started_at,
            ended_at=ended_at,
            success=True,
            reason=None,
        )

    def _run_turn_loop(
        self,
        session,
        issue: Issue,
        attempt: Optional[int],
        cancel_event: threading.Event,
        callback: UpdateCallback,
    ) -> None:
        max_turns = max(1, self._config.agent.max_turns)
        current_issue = issue
        turn_number = 1

        while True:
            if cancel_event.is_set():
                raise AppServerError("turn_cancelled")

            prompt = self._build_turn_prompt(current_issue, attempt, turn_number, max_turns)
            self._app_server.run_turn(
                session=session,
                prompt=prompt,
                issue=current_issue,
                on_message=callback,
                cancel_event=cancel_event,
            )

            refreshed = self._tracker.fetch_issue_states_by_ids([current_issue.id])
            if refreshed:
                current_issue = refreshed[0]

            if _normalize_state(current_issue.state) not in self._config.active_states:
                break
            if turn_number >= max_turns:
                break
            turn_number += 1

    def _build_turn_prompt(
        self,
        issue: Issue,
        attempt: Optional[int],
        turn_number: int,
        max_turns: int,
    ) -> str:
        if turn_number == 1:
            return render_prompt(self._config.prompt_template, issue, attempt=attempt)
        return (
            "Continuation guidance:\n\n"
            "- The previous Codex turn completed normally, but the issue is still in an active state.\n"
            f"- This is continuation turn #{turn_number} of {max_turns}.\n"
            "- Resume from current workspace state, do not restart.\n"
            "- Focus only on remaining work.\n"
        )


def _normalize_state(state: str) -> str:
    return state.strip().lower()

