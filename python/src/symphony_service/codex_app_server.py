"""Codex app-server JSON-lines protocol client."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any, Callable, Optional

from .config import ServiceConfig
from .errors import AppServerError
from .models import Issue
from .tracker import TrackerAdapter

LOGGER = logging.getLogger(__name__)
NON_INTERACTIVE_TOOL_INPUT_ANSWER = "This is a non-interactive session. Operator input is unavailable."

OnMessage = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class AppServerSession:
    process: subprocess.Popen[str]
    queue: "queue.Queue[object]"
    thread_id: str
    workspace: Path
    approval_policy: str | dict[str, object]
    thread_sandbox: str
    turn_sandbox_policy: dict[str, object]
    next_request_id: int


class CodexAppServerClient:
    def __init__(self, config: ServiceConfig, tracker: Optional[TrackerAdapter] = None) -> None:
        self._config = config
        self._tracker = tracker

    def start_session(self, workspace: Path) -> AppServerSession:
        self._validate_workspace(workspace)
        process = self._start_process(workspace)
        stream_queue: "queue.Queue[object]" = queue.Queue()
        reader = threading.Thread(
            target=_stream_reader,
            args=(process, stream_queue),
            name=f"codex-stream-{workspace.name}",
            daemon=True,
        )
        reader.start()

        session = AppServerSession(
            process=process,
            queue=stream_queue,
            thread_id="",
            workspace=workspace,
            approval_policy=self._config.codex.approval_policy,
            thread_sandbox=self._config.codex.thread_sandbox,
            turn_sandbox_policy=self._config.codex_turn_sandbox_policy(workspace),
            next_request_id=4,
        )

        try:
            self._send_message(
                session,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "capabilities": {"experimentalApi": True},
                        "clientInfo": {
                            "name": "symphony-orchestrator",
                            "title": "Symphony Orchestrator",
                            "version": "0.1.0",
                        },
                    },
                },
            )
            self._await_response(session, request_id=1, timeout_ms=self._config.codex.read_timeout_ms)
            self._send_message(session, {"method": "initialized", "params": {}})

            self._send_message(
                session,
                {
                    "method": "thread/start",
                    "id": 2,
                    "params": {
                        "approvalPolicy": session.approval_policy,
                        "sandbox": session.thread_sandbox,
                        "cwd": str(workspace.expanduser().resolve()),
                        "dynamicTools": self._tool_specs(),
                    },
                },
            )
            result = self._await_response(session, request_id=2, timeout_ms=self._config.codex.read_timeout_ms)
        except Exception:
            self.stop_session(session)
            raise

        thread_payload = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread_payload.get("id") if isinstance(thread_payload, dict) else None
        if not isinstance(thread_id, str) or not thread_id.strip():
            self.stop_session(session)
            raise AppServerError(f"invalid_thread_payload:{result}")
        session.thread_id = thread_id.strip()
        return session

    def stop_session(self, session: AppServerSession) -> None:
        process = session.process
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass

    def run_turn(
        self,
        session: AppServerSession,
        prompt: str,
        issue: Issue,
        on_message: Optional[OnMessage] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> dict[str, Any]:
        callback = on_message or (lambda _: None)
        request_id = session.next_request_id
        session.next_request_id += 1

        self._send_message(
            session,
            {
                "method": "turn/start",
                "id": request_id,
                "params": {
                    "threadId": session.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": str(session.workspace.expanduser().resolve()),
                    "title": f"{issue.identifier}: {issue.title}",
                    "approvalPolicy": session.approval_policy,
                    "sandboxPolicy": session.turn_sandbox_policy,
                },
            },
        )

        response = self._await_response(
            session,
            request_id=request_id,
            timeout_ms=self._config.codex.read_timeout_ms,
            on_message=callback,
        )
        turn = response.get("turn") if isinstance(response, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise AppServerError(f"invalid_turn_payload:{response}")

        session_id = f"{session.thread_id}-{turn_id}"
        callback(
            {
                "event": "session_started",
                "timestamp": datetime.now(timezone.utc),
                "session_id": session_id,
                "thread_id": session.thread_id,
                "turn_id": turn_id,
                "codex_app_server_pid": session.process.pid,
            }
        )

        auto_approve = session.approval_policy == "never"
        turn_deadline = time.monotonic() + (self._config.codex.turn_timeout_ms / 1000)

        while True:
            if cancel_event and cancel_event.is_set():
                raise AppServerError("turn_cancelled")
            if time.monotonic() > turn_deadline:
                raise AppServerError("turn_timeout")

            try:
                item = session.queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if item is _PROCESS_EXIT:
                code = session.process.poll()
                raise AppServerError(f"port_exit:{code}")

            if not isinstance(item, str):
                continue

            payload, raw_line = _decode_line(item)
            if payload is None:
                callback(
                    {
                        "event": "malformed",
                        "timestamp": datetime.now(timezone.utc),
                        "payload": raw_line,
                        "raw": raw_line,
                        "codex_app_server_pid": session.process.pid,
                    }
                )
                continue

            method = payload.get("method") if isinstance(payload, dict) else None
            usage = _extract_usage(payload)
            meta = {
                "timestamp": datetime.now(timezone.utc),
                "payload": payload,
                "raw": raw_line,
                "usage": usage,
                "codex_app_server_pid": session.process.pid,
            }

            if method == "turn/completed":
                callback({"event": "turn_completed", **meta, "session_id": session_id})
                return {
                    "session_id": session_id,
                    "thread_id": session.thread_id,
                    "turn_id": turn_id,
                    "result": payload.get("params"),
                }
            if method == "turn/failed":
                callback({"event": "turn_failed", **meta, "session_id": session_id})
                raise AppServerError(f"turn_failed:{payload.get('params')}")
            if method == "turn/cancelled":
                callback({"event": "turn_cancelled", **meta, "session_id": session_id})
                raise AppServerError(f"turn_cancelled:{payload.get('params')}")

            if isinstance(method, str):
                handled = self._maybe_handle_runtime_method(
                    session=session,
                    payload=payload,
                    raw_line=raw_line,
                    auto_approve=auto_approve,
                    on_message=callback,
                )
                if handled:
                    continue
                callback({"event": "notification", **meta, "session_id": session_id})

    def _maybe_handle_runtime_method(
        self,
        session: AppServerSession,
        payload: dict[str, Any],
        raw_line: str,
        auto_approve: bool,
        on_message: OnMessage,
    ) -> bool:
        method = payload.get("method")
        request_id = payload.get("id")
        if not isinstance(method, str):
            return False

        approval_map = {
            "item/commandExecution/requestApproval": "acceptForSession",
            "item/fileChange/requestApproval": "acceptForSession",
            "execCommandApproval": "approved_for_session",
            "applyPatchApproval": "approved_for_session",
        }

        if method in approval_map and request_id is not None:
            if not auto_approve:
                on_message(
                    {
                        "event": "approval_required",
                        "timestamp": datetime.now(timezone.utc),
                        "payload": payload,
                        "raw": raw_line,
                        "codex_app_server_pid": session.process.pid,
                    }
                )
                raise AppServerError(f"approval_required:{method}")
            self._send_message(session, {"id": request_id, "result": {"decision": approval_map[method]}})
            on_message(
                {
                    "event": "approval_auto_approved",
                    "timestamp": datetime.now(timezone.utc),
                    "payload": payload,
                    "raw": raw_line,
                    "codex_app_server_pid": session.process.pid,
                }
            )
            return True

        if method == "item/tool/requestUserInput" and request_id is not None:
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            answers = _tool_input_answers(params, auto_approve)
            self._send_message(session, {"id": request_id, "result": {"answers": answers}})
            on_message(
                {
                    "event": "tool_input_auto_answered",
                    "timestamp": datetime.now(timezone.utc),
                    "payload": payload,
                    "raw": raw_line,
                    "codex_app_server_pid": session.process.pid,
                }
            )
            return True

        if method == "item/tool/call" and request_id is not None:
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            tool_name = params.get("tool") or params.get("name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else params.get("arguments")
            result = self._execute_tool(tool_name, arguments)
            self._send_message(session, {"id": request_id, "result": result})
            on_message(
                {
                    "event": "tool_call_completed" if result.get("success") else "tool_call_failed",
                    "timestamp": datetime.now(timezone.utc),
                    "payload": payload,
                    "raw": raw_line,
                    "codex_app_server_pid": session.process.pid,
                }
            )
            return True

        if method.startswith("turn/") and _needs_input(payload):
            raise AppServerError(f"turn_input_required:{method}")
        return False

    def _execute_tool(self, tool_name: Any, arguments: Any) -> dict[str, Any]:
        if tool_name != "linear_graphql":
            return _tool_failure(
                {
                    "message": f"Unsupported dynamic tool: {tool_name!r}",
                    "supportedTools": ["linear_graphql"],
                }
            )
        if self._tracker is None:
            return _tool_failure({"message": "Tracker is unavailable for linear_graphql"})
        query = None
        variables: dict[str, Any] = {}
        if isinstance(arguments, str):
            query = arguments.strip()
        elif isinstance(arguments, dict):
            if isinstance(arguments.get("query"), str):
                query = arguments["query"].strip()
            if isinstance(arguments.get("variables"), dict):
                variables = arguments["variables"]
        if not query:
            return _tool_failure({"message": "linear_graphql requires a non-empty `query`."})
        try:
            payload = self._tracker.graphql_raw(query, variables)
        except Exception as exc:  # noqa: BLE001
            return _tool_failure({"message": "Linear GraphQL tool execution failed.", "reason": str(exc)})
        return _tool_success(payload)

    def _await_response(
        self,
        session: AppServerSession,
        request_id: int,
        timeout_ms: int,
        on_message: Optional[OnMessage] = None,
    ) -> dict[str, Any]:
        callback = on_message or (lambda _: None)
        deadline = time.monotonic() + (timeout_ms / 1000)

        while True:
            if time.monotonic() > deadline:
                raise AppServerError("response_timeout")
            try:
                item = session.queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if item is _PROCESS_EXIT:
                code = session.process.poll()
                raise AppServerError(f"port_exit:{code}")
            if not isinstance(item, str):
                continue

            payload, raw_line = _decode_line(item)
            if payload is None:
                _log_non_json_stream_line(raw_line, "response stream")
                continue

            payload_id = payload.get("id")
            if payload_id == request_id and "error" in payload:
                raise AppServerError(f"response_error:{payload['error']}")
            if payload_id == request_id and "result" in payload:
                result = payload["result"]
                if not isinstance(result, dict):
                    raise AppServerError(f"response_error:{payload}")
                return result

            callback(
                {
                    "event": "other_message",
                    "timestamp": datetime.now(timezone.utc),
                    "payload": payload,
                    "raw": raw_line,
                    "usage": _extract_usage(payload),
                    "codex_app_server_pid": session.process.pid,
                }
            )

    def _send_message(self, session: AppServerSession, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=True) + "\n"
        if session.process.stdin is None:
            raise AppServerError("process_stdin_closed")
        try:
            session.process.stdin.write(line)
            session.process.stdin.flush()
        except OSError as exc:
            raise AppServerError(f"port_write_failed:{exc}") from exc

    def _start_process(self, workspace: Path) -> subprocess.Popen[str]:
        shell_cmd = _shell_command(self._config.codex.command)
        try:
            process = subprocess.Popen(
                shell_cmd,
                cwd=workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise AppServerError(f"codex_process_start_failed:{exc}") from exc
        return process

    def _validate_workspace(self, workspace: Path) -> None:
        root = self._config.workspace_root.expanduser().resolve()
        target = workspace.expanduser().resolve()
        if target == root:
            raise AppServerError(f"invalid_workspace_cwd:workspace_root:{target}")
        if root not in target.parents:
            raise AppServerError(f"invalid_workspace_cwd:outside_workspace_root:{target}:{root}")

    def _tool_specs(self) -> list[dict[str, Any]]:
        if self._config.tracker.kind != "linear":
            return []
        return [
            {
                "name": "linear_graphql",
                "description": "Execute a raw GraphQL query or mutation against Linear using Symphony auth.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "variables": {
                            "type": ["object", "null"],
                            "additionalProperties": True,
                        },
                    },
                },
            }
        ]


_PROCESS_EXIT = object()


def _stream_reader(process: subprocess.Popen[str], output_queue: "queue.Queue[object]") -> None:
    if process.stdout is None:
        output_queue.put(_PROCESS_EXIT)
        return
    try:
        for line in process.stdout:
            output_queue.put(line.rstrip("\n"))
    finally:
        output_queue.put(_PROCESS_EXIT)


def _decode_line(raw_line: str) -> tuple[Optional[dict[str, Any]], str]:
    text = raw_line.strip()
    if not text:
        return None, raw_line
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, raw_line
    if not isinstance(payload, dict):
        return None, raw_line
    return payload, raw_line


def _log_non_json_stream_line(data: str, stream_label: str) -> None:
    text = (data or "").strip()
    if not text:
        return
    truncated = text[:1000]
    if any(keyword in truncated.lower() for keyword in ("error", "warn", "failed", "fatal", "exception", "panic")):
        LOGGER.warning("Codex %s output: %s", stream_label, truncated)
    else:
        LOGGER.debug("Codex %s output: %s", stream_label, truncated)


def _shell_command(command: str) -> list[str]:
    if os.name == "nt":
        if which("powershell"):
            return ["powershell", "-Command", command]
        if which("cmd"):
            return ["cmd", "/c", command]
        if which("bash"):
            return ["bash", "-lc", command]
        if which("sh"):
            return ["sh", "-lc", command]
        return ["cmd", "/c", command]

    if which("bash"):
        return ["bash", "-lc", command]
    if which("sh"):
        return ["sh", "-lc", command]
    return ["cmd", "/c", command]


def _extract_usage(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    for candidate in (
        payload.get("usage"),
        _path(payload, "params", "usage"),
        _path(payload, "params", "tokenUsage", "total"),
        _path(payload, "params", "msg", "payload", "info", "total_token_usage"),
        _path(payload, "params", "msg", "info", "total_token_usage"),
    ):
        if isinstance(candidate, dict):
            return candidate
    return None


def _path(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _needs_input(payload: dict[str, Any]) -> bool:
    method = payload.get("method")
    if method in {
        "turn/input_required",
        "turn/needs_input",
        "turn/need_input",
        "turn/request_input",
        "turn/request_response",
        "turn/provide_input",
        "turn/approval_required",
    }:
        return True
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    for key in ("requiresInput", "needsInput", "input_required", "inputRequired"):
        if payload.get(key) is True or params.get(key) is True:
            return True
    if payload.get("type") in {"input_required", "needs_input"}:
        return True
    return False


def _tool_input_answers(params: dict[str, Any], auto_approve: bool) -> dict[str, dict[str, list[str]]]:
    questions = params.get("questions")
    if not isinstance(questions, list):
        return {}
    answers: dict[str, dict[str, list[str]]] = {}
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id:
            continue
        answer_text = NON_INTERACTIVE_TOOL_INPUT_ANSWER
        if auto_approve:
            options = question.get("options")
            if isinstance(options, list):
                approval = _approval_option_label(options)
                if approval:
                    answer_text = approval
        answers[question_id] = {"answers": [answer_text]}
    return answers


def _approval_option_label(options: list[Any]) -> Optional[str]:
    labels: list[str] = []
    for option in options:
        if isinstance(option, dict) and isinstance(option.get("label"), str):
            labels.append(option["label"])
    for preferred in ("Approve this Session", "Approve Once"):
        if preferred in labels:
            return preferred
    for label in labels:
        normalized = label.strip().lower()
        if normalized.startswith("approve") or normalized.startswith("allow"):
            return label
    return labels[0] if labels else None


def _tool_success(payload: Any) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=True, indent=2) if isinstance(payload, (dict, list)) else str(payload)
    success = True
    if isinstance(payload, dict) and isinstance(payload.get("errors"), list) and payload["errors"]:
        success = False
    return {
        "success": success,
        "contentItems": [
            {
                "type": "inputText",
                "text": text,
            }
        ],
    }


def _tool_failure(payload: Any) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=True, indent=2) if isinstance(payload, (dict, list)) else str(payload)
    return {
        "success": False,
        "contentItems": [
            {
                "type": "inputText",
                "text": text,
            }
        ],
    }
