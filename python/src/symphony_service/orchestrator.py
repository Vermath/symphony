"""Polling orchestrator and runtime state machine."""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from .agent_runner import AgentRunner
from .codex_app_server import CodexAppServerClient
from .config import ServiceConfig
from .errors import ConfigValidationError, TrackerError, WorkflowError
from .models import CodexUsageDelta, Issue, RetryEntry, RunningEntry, TokenTotals, WorkerResult
from .tracker import TrackerAdapter, build_tracker
from .workflow import WorkflowSnapshot, WorkflowStore
from .workspace import WorkspaceManager

LOGGER = logging.getLogger(__name__)

CONTINUATION_RETRY_DELAY_MS = 1_000
FAILURE_RETRY_BASE_MS = 10_000


class Orchestrator:
    def __init__(
        self,
        workflow_store: WorkflowStore,
        max_workers: Optional[int] = None,
    ) -> None:
        self._workflow_store = workflow_store
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()

        self._codex_updates: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
        self._worker_completions: "queue.Queue[tuple[str, Future[WorkerResult]]]" = queue.Queue()
        self._executor = ThreadPoolExecutor(max_workers=max_workers or 32, thread_name_prefix="symphony-worker")

        self._config: Optional[ServiceConfig] = None
        self._tracker: Optional[TrackerAdapter] = None
        self._workspace_manager: Optional[WorkspaceManager] = None
        self._app_server: Optional[CodexAppServerClient] = None
        self._runner: Optional[AgentRunner] = None

        self._running: dict[str, RunningEntry] = {}
        self._claimed: set[str] = set()
        self._retry_attempts: dict[str, RetryEntry] = {}
        self._completed: set[str] = set()

        self._codex_totals = TokenTotals()
        self._codex_rate_limits: Optional[dict[str, Any]] = None

        self._poll_interval_ms = 30_000
        self._max_concurrent_agents = 10
        self._next_poll_due_at_ms: Optional[int] = None
        self._poll_check_in_progress = False

    def start(self) -> None:
        self._initialize()
        self._run_loop()

    def stop(self) -> None:
        self._stop_event.set()
        self._refresh_event.set()
        with self._lock:
            for running in self._running.values():
                running.cancel_event.set()
                running.future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def request_refresh(self) -> dict[str, Any]:
        now_ms = _monotonic_ms()
        with self._lock:
            already_due = self._next_poll_due_at_ms is not None and self._next_poll_due_at_ms <= now_ms
            coalesced = self._poll_check_in_progress or already_due
            self._refresh_event.set()
            return {
                "queued": True,
                "coalesced": coalesced,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "operations": ["poll", "reconcile"],
            }

    def snapshot(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        now_ms = _monotonic_ms()
        with self._lock:
            running = []
            for entry in self._running.values():
                runtime_seconds = max(0, int((now - entry.started_at).total_seconds()))
                running.append(
                    {
                        "issue_id": entry.issue_id,
                        "identifier": entry.identifier,
                        "state": entry.issue.state,
                        "session_id": entry.session_id,
                        "codex_app_server_pid": entry.codex_app_server_pid,
                        "codex_input_tokens": entry.codex_input_tokens,
                        "codex_output_tokens": entry.codex_output_tokens,
                        "codex_total_tokens": entry.codex_total_tokens,
                        "turn_count": entry.turn_count,
                        "started_at": entry.started_at,
                        "last_codex_timestamp": entry.last_codex_timestamp,
                        "last_codex_message": entry.last_codex_message,
                        "last_codex_event": entry.last_codex_event,
                        "runtime_seconds": runtime_seconds,
                    }
                )

            retrying = []
            for retry in self._retry_attempts.values():
                retrying.append(
                    {
                        "issue_id": retry.issue_id,
                        "attempt": retry.attempt,
                        "due_in_ms": max(0, retry.due_at_monotonic_ms - now_ms),
                        "identifier": retry.identifier,
                        "error": retry.error,
                    }
                )

            return {
                "running": running,
                "retrying": retrying,
                "codex_totals": {
                    "input_tokens": self._codex_totals.input_tokens,
                    "output_tokens": self._codex_totals.output_tokens,
                    "total_tokens": self._codex_totals.total_tokens,
                    "seconds_running": self._codex_totals.seconds_running,
                },
                "rate_limits": self._codex_rate_limits,
                "polling": {
                    "checking?": self._poll_check_in_progress,
                    "next_poll_in_ms": None
                    if self._next_poll_due_at_ms is None
                    else max(0, self._next_poll_due_at_ms - now_ms),
                    "poll_interval_ms": self._poll_interval_ms,
                },
            }

    def _initialize(self) -> None:
        workflow = self._workflow_store.load_initial()
        config = ServiceConfig.from_workflow(workflow)
        config.validate_dispatch()
        self._apply_config(config)
        self._startup_terminal_cleanup()

    def _apply_config(self, config: ServiceConfig) -> None:
        self._config = config
        self._poll_interval_ms = config.poll_interval_ms
        self._max_concurrent_agents = config.agent.max_concurrent_agents
        self._tracker = build_tracker(config)
        self._workspace_manager = WorkspaceManager(config)
        self._app_server = CodexAppServerClient(config, tracker=self._tracker)
        self._runner = AgentRunner(config, self._tracker, self._workspace_manager, self._app_server)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            cycle_start = _monotonic_ms()
            with self._lock:
                self._poll_check_in_progress = True
                self._next_poll_due_at_ms = None

            self._refresh_runtime_config()
            self._drain_codex_updates()
            self._process_worker_completions()
            self._reconcile_running_issues()
            self._run_due_retries()
            self._dispatch_cycle()
            self._drain_codex_updates()
            self._process_worker_completions()

            interval = self._poll_interval_ms
            now_ms = _monotonic_ms()
            elapsed = max(0, now_ms - cycle_start)
            sleep_ms = max(0, interval - elapsed)
            with self._lock:
                self._poll_check_in_progress = False
                self._next_poll_due_at_ms = now_ms + sleep_ms

            self._refresh_event.wait(timeout=sleep_ms / 1000)
            self._refresh_event.clear()

    def _refresh_runtime_config(self) -> None:
        try:
            snapshot: WorkflowSnapshot = self._workflow_store.refresh()
        except WorkflowError as exc:
            LOGGER.error("Workflow reload failed: %s", exc, extra={"component": "workflow", "error": str(exc)})
            return

        if snapshot.error is not None:
            LOGGER.error(
                "Workflow reload failed; keeping last known good config: %s",
                snapshot.error,
                extra={"component": "workflow", "error": str(snapshot.error)},
            )
        if not snapshot.changed:
            return

        try:
            new_config = ServiceConfig.from_workflow(snapshot.workflow)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "Workflow changed but new config is invalid; keeping previous config: %s",
                exc,
                extra={"component": "workflow", "error": str(exc)},
            )
            return

        with self._lock:
            self._apply_config(new_config)
            LOGGER.info("Reloaded workflow and runtime config", extra={"component": "workflow"})

    def _dispatch_cycle(self) -> None:
        config = self._require_config()
        tracker = self._require_tracker()

        try:
            config.validate_dispatch()
        except ConfigValidationError as exc:
            LOGGER.error("Dispatch preflight failed: %s", exc, extra={"component": "orchestrator", "error": str(exc)})
            return

        try:
            issues = tracker.fetch_candidate_issues()
        except TrackerError as exc:
            LOGGER.error("Tracker poll failed: %s", exc, extra={"component": "tracker", "error": str(exc)})
            return

        sorted_issues = sorted(issues, key=_dispatch_sort_key)
        for issue in sorted_issues:
            if self._available_slots() <= 0:
                break
            if not self._should_dispatch(issue):
                continue
            self._dispatch_issue(issue, attempt=None)

    def _run_due_retries(self) -> None:
        tracker = self._require_tracker()
        config = self._require_config()
        now_ms = _monotonic_ms()
        with self._lock:
            due_ids = [issue_id for issue_id, entry in self._retry_attempts.items() if entry.due_at_monotonic_ms <= now_ms]
            due_entries = [self._retry_attempts.pop(issue_id) for issue_id in due_ids]

        if not due_entries:
            return

        try:
            candidates = tracker.fetch_candidate_issues()
        except TrackerError as exc:
            for entry in due_entries:
                self._schedule_issue_retry(
                    issue_id=entry.issue_id,
                    attempt=entry.attempt + 1,
                    identifier=entry.identifier,
                    error=f"retry poll failed: {exc}",
                )
            return

        by_id = {issue.id: issue for issue in candidates}
        for entry in due_entries:
            issue = by_id.get(entry.issue_id)
            if issue is None:
                with self._lock:
                    self._claimed.discard(entry.issue_id)
                continue
            if _normalize_state(issue.state) in config.terminal_states:
                self._workspace_manager_or_raise().remove_issue_workspaces(issue.identifier)
                with self._lock:
                    self._claimed.discard(entry.issue_id)
                continue
            if not self._retry_candidate(issue):
                with self._lock:
                    self._claimed.discard(entry.issue_id)
                continue
            if self._available_slots() <= 0 or not self._state_slots_available(issue):
                self._schedule_issue_retry(
                    issue_id=entry.issue_id,
                    attempt=entry.attempt + 1,
                    identifier=entry.identifier,
                    error="no available orchestrator slots",
                )
                continue
            self._dispatch_issue(issue, attempt=entry.attempt)

    def _process_worker_completions(self) -> None:
        while True:
            try:
                issue_id, future = self._worker_completions.get_nowait()
            except queue.Empty:
                return

            with self._lock:
                running = self._running.get(issue_id)
                if running is None or running.future is not future:
                    continue
                entry = self._running.pop(issue_id)

            runtime_seconds = max(0, int((datetime.now(timezone.utc) - entry.started_at).total_seconds()))
            with self._lock:
                self._codex_totals.seconds_running = max(0, self._codex_totals.seconds_running + runtime_seconds)

            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = WorkerResult(
                    issue_id=entry.issue_id,
                    identifier=entry.identifier,
                    started_at=entry.started_at,
                    ended_at=datetime.now(timezone.utc),
                    success=False,
                    reason=f"agent exited: {exc}",
                )

            if result.success:
                with self._lock:
                    self._completed.add(entry.issue_id)
                self._schedule_issue_retry(
                    issue_id=entry.issue_id,
                    attempt=1,
                    identifier=entry.identifier,
                    error=None,
                    delay_type="continuation",
                )
            else:
                next_attempt = entry.retry_attempt + 1 if entry.retry_attempt > 0 else None
                self._schedule_issue_retry(
                    issue_id=entry.issue_id,
                    attempt=next_attempt,
                    identifier=entry.identifier,
                    error=result.reason or "agent exited",
                )

    def _reconcile_running_issues(self) -> None:
        self._reconcile_stalled_runs()

        with self._lock:
            running_ids = list(self._running.keys())
        if not running_ids:
            return

        tracker = self._require_tracker()
        config = self._require_config()
        try:
            refreshed = tracker.fetch_issue_states_by_ids(running_ids)
        except TrackerError as exc:
            LOGGER.debug("Failed to refresh running issue states: %s", exc, extra={"component": "orchestrator"})
            return

        refreshed_by_id = {issue.id: issue for issue in refreshed}
        for issue_id in running_ids:
            issue = refreshed_by_id.get(issue_id)
            if issue is None:
                continue
            state = _normalize_state(issue.state)
            if state in config.terminal_states:
                LOGGER.info(
                    "Issue moved to terminal state; stopping active agent",
                    extra={"component": "orchestrator", "issue_id": issue.id, "issue_identifier": issue.identifier},
                )
                self._terminate_running_issue(issue_id=issue.id, cleanup_workspace=True)
            elif state in config.active_states and issue.assigned_to_worker:
                with self._lock:
                    running = self._running.get(issue.id)
                    if running:
                        running.issue = issue
            else:
                LOGGER.info(
                    "Issue moved to non-active state; stopping active agent",
                    extra={"component": "orchestrator", "issue_id": issue.id, "issue_identifier": issue.identifier},
                )
                self._terminate_running_issue(issue_id=issue.id, cleanup_workspace=False)

    def _reconcile_stalled_runs(self) -> None:
        config = self._require_config()
        timeout_ms = config.codex.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = datetime.now(timezone.utc)
        with self._lock:
            running_items = list(self._running.items())

        for issue_id, entry in running_items:
            last_activity = entry.last_codex_timestamp or entry.started_at
            elapsed_ms = max(0, int((now - last_activity).total_seconds() * 1000))
            if elapsed_ms <= timeout_ms:
                continue
            LOGGER.warning(
                "Issue stalled; restarting with backoff",
                extra={
                    "component": "orchestrator",
                    "issue_id": issue_id,
                    "issue_identifier": entry.identifier,
                    "session_id": entry.session_id,
                },
            )
            next_attempt = entry.retry_attempt + 1 if entry.retry_attempt > 0 else None
            self._terminate_running_issue(issue_id, cleanup_workspace=False)
            self._schedule_issue_retry(
                issue_id=issue_id,
                attempt=next_attempt,
                identifier=entry.identifier,
                error=f"stalled for {elapsed_ms}ms without codex activity",
            )

    def _terminate_running_issue(self, issue_id: str, cleanup_workspace: bool) -> None:
        with self._lock:
            entry = self._running.pop(issue_id, None)
            self._retry_attempts.pop(issue_id, None)
            self._claimed.discard(issue_id)
        if entry is None:
            return

        runtime_seconds = max(0, int((datetime.now(timezone.utc) - entry.started_at).total_seconds()))
        with self._lock:
            self._codex_totals.seconds_running = max(0, self._codex_totals.seconds_running + runtime_seconds)

        entry.cancel_event.set()
        entry.future.cancel()
        if cleanup_workspace:
            self._workspace_manager_or_raise().remove_issue_workspaces(entry.identifier)

    def _dispatch_issue(self, issue: Issue, attempt: Optional[int]) -> None:
        revalidated = self._revalidate_issue(issue)
        if revalidated is None:
            return
        issue = revalidated

        runner = self._runner_or_raise()
        cancel_event = threading.Event()

        def _on_update(message: dict[str, Any]) -> None:
            self._codex_updates.put((issue.id, message))

        try:
            future: Future[WorkerResult] = self._executor.submit(
                runner.run,
                issue,
                attempt,
                cancel_event,
                _on_update,
            )
        except RuntimeError as exc:
            self._schedule_issue_retry(
                issue_id=issue.id,
                attempt=(attempt + 1) if isinstance(attempt, int) else None,
                identifier=issue.identifier,
                error=f"failed to spawn agent: {exc}",
            )
            return

        future.add_done_callback(lambda f, issue_id=issue.id: self._worker_completions.put((issue_id, f)))

        running_entry = RunningEntry(
            issue=issue,
            issue_id=issue.id,
            identifier=issue.identifier,
            workspace_path=self._workspace_manager_or_raise().workspace_path_for_issue(issue.identifier),
            future=future,
            cancel_event=cancel_event,
            retry_attempt=attempt if isinstance(attempt, int) and attempt > 0 else 0,
            started_at=datetime.now(timezone.utc),
        )

        with self._lock:
            self._running[issue.id] = running_entry
            self._claimed.add(issue.id)
            self._retry_attempts.pop(issue.id, None)

        LOGGER.info(
            "Dispatching issue to agent",
            extra={"component": "orchestrator", "issue_id": issue.id, "issue_identifier": issue.identifier},
        )

    def _revalidate_issue(self, issue: Issue) -> Optional[Issue]:
        tracker = self._require_tracker()
        try:
            refreshed = tracker.fetch_issue_states_by_ids([issue.id])
        except TrackerError as exc:
            LOGGER.warning(
                "Skipping dispatch; issue refresh failed: %s",
                exc,
                extra={"component": "orchestrator", "issue_id": issue.id, "issue_identifier": issue.identifier},
            )
            return None
        if not refreshed:
            return None
        refreshed_issue = refreshed[0]
        if not self._retry_candidate(refreshed_issue):
            return None
        return refreshed_issue

    def _retry_candidate(self, issue: Issue) -> bool:
        config = self._require_config()
        state = _normalize_state(issue.state)
        if state not in config.active_states:
            return False
        if state in config.terminal_states:
            return False
        if not issue.assigned_to_worker:
            return False
        return not self._todo_blocked(issue)

    def _should_dispatch(self, issue: Issue) -> bool:
        if not self._retry_candidate(issue):
            return False
        with self._lock:
            if issue.id in self._claimed or issue.id in self._running:
                return False
        if self._available_slots() <= 0:
            return False
        return self._state_slots_available(issue)

    def _todo_blocked(self, issue: Issue) -> bool:
        config = self._require_config()
        if _normalize_state(issue.state) != "todo":
            return False
        for blocker in issue.blocked_by:
            state = blocker.get("state") if isinstance(blocker, dict) else None
            if not isinstance(state, str):
                return True
            if _normalize_state(state) not in config.terminal_states:
                return True
        return False

    def _state_slots_available(self, issue: Issue) -> bool:
        config = self._require_config()
        with self._lock:
            running_for_state = sum(
                1 for entry in self._running.values() if _normalize_state(entry.issue.state) == _normalize_state(issue.state)
            )
        return running_for_state < config.max_concurrent_for_state(issue.state)

    def _available_slots(self) -> int:
        with self._lock:
            return max(self._max_concurrent_agents - len(self._running), 0)

    def _schedule_issue_retry(
        self,
        issue_id: str,
        attempt: Optional[int],
        identifier: str,
        error: Optional[str],
        delay_type: Optional[str] = None,
    ) -> None:
        with self._lock:
            previous = self._retry_attempts.get(issue_id)
            next_attempt = attempt if isinstance(attempt, int) and attempt > 0 else ((previous.attempt + 1) if previous else 1)
            due_at = _monotonic_ms() + self._retry_delay(next_attempt, delay_type)
            self._retry_attempts[issue_id] = RetryEntry(
                issue_id=issue_id,
                identifier=identifier or (previous.identifier if previous else issue_id),
                attempt=next_attempt,
                due_at_monotonic_ms=due_at,
                error=error or (previous.error if previous else None),
            )
            self._claimed.add(issue_id)

        LOGGER.warning(
            "Scheduling retry attempt=%s in %sms error=%s",
            next_attempt,
            max(0, due_at - _monotonic_ms()),
            error,
            extra={"component": "orchestrator", "issue_id": issue_id, "issue_identifier": identifier},
        )

    def _retry_delay(self, attempt: int, delay_type: Optional[str]) -> int:
        config = self._require_config()
        if delay_type == "continuation" and attempt == 1:
            return CONTINUATION_RETRY_DELAY_MS
        power = min(attempt - 1, 10)
        delay = FAILURE_RETRY_BASE_MS * (2**power)
        return min(delay, config.agent.max_retry_backoff_ms)

    def _startup_terminal_cleanup(self) -> None:
        tracker = self._require_tracker()
        config = self._require_config()
        try:
            terminal_issues = tracker.fetch_issues_by_states(config.tracker.terminal_states)
        except TrackerError as exc:
            LOGGER.warning(
                "Skipping startup terminal workspace cleanup: %s",
                exc,
                extra={"component": "orchestrator", "error": str(exc)},
            )
            return
        workspace = self._workspace_manager_or_raise()
        for issue in terminal_issues:
            workspace.remove_issue_workspaces(issue.identifier)

    def _drain_codex_updates(self) -> None:
        while True:
            try:
                issue_id, update = self._codex_updates.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                running = self._running.get(issue_id)
                if running is None:
                    continue

                timestamp = update.get("timestamp")
                if isinstance(timestamp, datetime):
                    running.last_codex_timestamp = timestamp
                event = update.get("event")
                running.last_codex_event = str(event) if event is not None else running.last_codex_event
                payload = update.get("payload") if isinstance(update.get("payload"), dict) else None
                raw = update.get("raw")
                if isinstance(raw, str):
                    running.last_codex_message = raw[:2000]
                elif isinstance(payload, dict):
                    running.last_codex_message = str(payload)[:2000]

                if isinstance(update.get("session_id"), str):
                    previous_session = running.session_id
                    running.session_id = update["session_id"]
                    if running.last_codex_event == "session_started" and previous_session != running.session_id:
                        running.turn_count += 1
                if update.get("codex_app_server_pid") is not None:
                    running.codex_app_server_pid = str(update["codex_app_server_pid"])

                usage = update.get("usage")
                delta = self._usage_delta(running, usage)
                running.codex_input_tokens += delta.input_tokens
                running.codex_output_tokens += delta.output_tokens
                running.codex_total_tokens += delta.total_tokens
                self._codex_totals.apply(delta)

                maybe_rate_limits = self._extract_rate_limits(update)
                if maybe_rate_limits:
                    self._codex_rate_limits = maybe_rate_limits

    def _usage_delta(self, running: RunningEntry, usage: Any) -> CodexUsageDelta:
        if not isinstance(usage, dict):
            return CodexUsageDelta()
        input_total = _usage_value(usage, ["input_tokens", "prompt_tokens", "inputTokens", "promptTokens"])
        output_total = _usage_value(
            usage, ["output_tokens", "completion_tokens", "outputTokens", "completionTokens"]
        )
        total_total = _usage_value(usage, ["total_tokens", "total", "totalTokens"])

        input_delta = _delta_from_reported(input_total, running.codex_last_reported_input_tokens)
        output_delta = _delta_from_reported(output_total, running.codex_last_reported_output_tokens)
        total_delta = _delta_from_reported(total_total, running.codex_last_reported_total_tokens)

        if input_total is not None:
            running.codex_last_reported_input_tokens = max(running.codex_last_reported_input_tokens, input_total)
        if output_total is not None:
            running.codex_last_reported_output_tokens = max(running.codex_last_reported_output_tokens, output_total)
        if total_total is not None:
            running.codex_last_reported_total_tokens = max(running.codex_last_reported_total_tokens, total_total)

        return CodexUsageDelta(input_tokens=input_delta, output_tokens=output_delta, total_tokens=total_delta)

    @staticmethod
    def _extract_rate_limits(update: dict[str, Any]) -> Optional[dict[str, Any]]:
        for key in ("rate_limits", "rateLimits"):
            value = update.get(key)
            if isinstance(value, dict):
                return value
        payload = update.get("payload")
        if isinstance(payload, dict):
            if isinstance(payload.get("rate_limits"), dict):
                return payload["rate_limits"]
            if isinstance(payload.get("rateLimits"), dict):
                return payload["rateLimits"]
            params = payload.get("params")
            if isinstance(params, dict):
                if isinstance(params.get("rateLimits"), dict):
                    return params["rateLimits"]
                if isinstance(params.get("rate_limits"), dict):
                    return params["rate_limits"]
        return None

    def _require_config(self) -> ServiceConfig:
        if self._config is None:
            raise RuntimeError("orchestrator_not_initialized")
        return self._config

    def _require_tracker(self) -> TrackerAdapter:
        if self._tracker is None:
            raise RuntimeError("orchestrator_not_initialized")
        return self._tracker

    def _workspace_manager_or_raise(self) -> WorkspaceManager:
        if self._workspace_manager is None:
            raise RuntimeError("orchestrator_not_initialized")
        return self._workspace_manager

    def _runner_or_raise(self) -> AgentRunner:
        if self._runner is None:
            raise RuntimeError("orchestrator_not_initialized")
        return self._runner


def _usage_value(usage: dict[str, Any], keys: list[str]) -> Optional[int]:
    for key in keys:
        raw = usage.get(key)
        if isinstance(raw, int) and raw >= 0:
            return raw
        if isinstance(raw, str):
            try:
                parsed = int(raw.strip())
            except ValueError:
                continue
            if parsed >= 0:
                return parsed
    return None


def _delta_from_reported(next_total: Optional[int], previous_total: int) -> int:
    if next_total is None:
        return 0
    if next_total < previous_total:
        return 0
    return max(0, next_total - previous_total)


def _normalize_state(state: str) -> str:
    return state.strip().lower()


def _dispatch_sort_key(issue: Issue) -> tuple[int, int, str]:
    priority = issue.priority if isinstance(issue.priority, int) and 1 <= issue.priority <= 4 else 5
    created = issue.created_at
    created_sort = int(created.timestamp() * 1_000_000) if created else 9_223_372_036_854_775_807
    return (priority, created_sort, issue.identifier or issue.id)


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)

