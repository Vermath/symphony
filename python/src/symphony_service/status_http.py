"""Optional HTTP observability surface."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from .orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)


class ObservabilityServer:
    def __init__(self, orchestrator: Orchestrator, host: str, port: Optional[int]) -> None:
        self._orchestrator = orchestrator
        self._host = host
        self._port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> Optional[int]:
        if self._port is None:
            return None

        handler = self._build_handler(self._orchestrator)
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="symphony-http", daemon=True)
        self._thread.start()
        bound_port = self._server.server_address[1]
        LOGGER.info("HTTP observability server started", extra={"component": "http", "event": f"port:{bound_port}"})
        return bound_port

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None

    @property
    def bound_port(self) -> Optional[int]:
        if self._server is None:
            return None
        return self._server.server_address[1]

    @staticmethod
    def _build_handler(orchestrator: Orchestrator):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    payload = _state_payload(orchestrator)
                    html = (
                        "<!doctype html><html><head><meta charset='utf-8'>"
                        "<title>Symphony Dashboard</title>"
                        "<style>body{font-family:Menlo,Consolas,monospace;margin:24px;"
                        "background:#f4efe6;color:#1f1d1a}pre{background:#fffdf8;border:1px solid #d8cfbf;"
                        "padding:16px;border-radius:12px;overflow:auto}</style></head><body>"
                        "<h1>Symphony Dashboard</h1>"
                        f"<pre>{_html_escape(json.dumps(payload, indent=2))}</pre></body></html>"
                    )
                    self._send_text(HTTPStatus.OK, html, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/v1/state":
                    self._send_json(HTTPStatus.OK, _state_payload(orchestrator))
                    return
                if parsed.path.startswith("/api/v1/"):
                    identifier = parsed.path[len("/api/v1/") :]
                    if not identifier:
                        self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Route not found")
                        return
                    payload = _issue_payload(orchestrator, identifier)
                    if payload is None:
                        self._send_error_json(HTTPStatus.NOT_FOUND, "issue_not_found", "Issue not found")
                        return
                    self._send_json(HTTPStatus.OK, payload)
                    return
                self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Route not found")

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/api/v1/refresh":
                    payload = orchestrator.request_refresh()
                    self._send_json(HTTPStatus.ACCEPTED, payload)
                    return
                if parsed.path.startswith("/api/v1/"):
                    self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")
                    return
                self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Route not found")

            def do_PUT(self) -> None:  # noqa: N802
                self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")

            def do_DELETE(self) -> None:  # noqa: N802
                self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")

            def log_message(self, fmt: str, *args) -> None:  # noqa: A003
                LOGGER.debug("HTTP %s", fmt % args, extra={"component": "http"})

            def _send_json(self, status: HTTPStatus, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(status.value)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_text(self, status: HTTPStatus, body: str, content_type: str) -> None:
                data = body.encode("utf-8")
                self.send_response(status.value)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_error_json(self, status: HTTPStatus, code: str, message: str) -> None:
                self._send_json(status, {"error": {"code": code, "message": message}})

        return Handler


def _state_payload(orchestrator: Orchestrator) -> dict:
    snapshot = orchestrator.snapshot()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "generated_at": generated_at,
        "counts": {
            "running": len(snapshot["running"]),
            "retrying": len(snapshot["retrying"]),
        },
        "running": [_running_payload(entry) for entry in snapshot["running"]],
        "retrying": [_retry_payload(entry) for entry in snapshot["retrying"]],
        "codex_totals": snapshot["codex_totals"],
        "rate_limits": snapshot["rate_limits"],
        "polling": snapshot.get("polling", {}),
    }


def _issue_payload(orchestrator: Orchestrator, identifier: str) -> Optional[dict]:
    snapshot = orchestrator.snapshot()
    running = next((item for item in snapshot["running"] if item.get("identifier") == identifier), None)
    retry = next((item for item in snapshot["retrying"] if item.get("identifier") == identifier), None)
    if running is None and retry is None:
        return None

    issue_id = running["issue_id"] if running else retry["issue_id"]
    status = "running" if running is not None else "retrying"
    return {
        "issue_identifier": identifier,
        "issue_id": issue_id,
        "status": status,
        "workspace": {"path": None},
        "attempts": {
            "restart_count": max(0, (retry.get("attempt") or 0) - 1) if retry else 0,
            "current_retry_attempt": retry.get("attempt") if retry else 0,
        },
        "running": _running_payload(running) if running else None,
        "retry": _retry_payload(retry) if retry else None,
        "logs": {"codex_session_logs": []},
        "recent_events": _recent_events_payload(running) if running else [],
        "last_error": retry.get("error") if retry else None,
        "tracked": {},
    }


def _running_payload(entry: dict) -> dict:
    return {
        "issue_id": entry.get("issue_id"),
        "issue_identifier": entry.get("identifier"),
        "state": entry.get("state"),
        "session_id": entry.get("session_id"),
        "turn_count": entry.get("turn_count", 0),
        "last_event": entry.get("last_codex_event"),
        "last_message": entry.get("last_codex_message"),
        "started_at": _iso(entry.get("started_at")),
        "last_event_at": _iso(entry.get("last_codex_timestamp")),
        "tokens": {
            "input_tokens": entry.get("codex_input_tokens", 0),
            "output_tokens": entry.get("codex_output_tokens", 0),
            "total_tokens": entry.get("codex_total_tokens", 0),
        },
    }


def _retry_payload(entry: dict) -> dict:
    due_in_ms = entry.get("due_in_ms", 0)
    return {
        "issue_id": entry.get("issue_id"),
        "issue_identifier": entry.get("identifier"),
        "attempt": entry.get("attempt", 0),
        "due_at": _iso(datetime.now(timezone.utc) + _seconds_delta(due_in_ms)),
        "error": entry.get("error"),
    }


def _recent_events_payload(running: dict) -> list[dict]:
    at = _iso(running.get("last_codex_timestamp"))
    if not at:
        return []
    return [
        {
            "at": at,
            "event": running.get("last_codex_event"),
            "message": running.get("last_codex_message"),
        }
    ]


def _seconds_delta(ms: int):
    from datetime import timedelta

    return timedelta(seconds=max(0, int(ms / 1000)))


def _iso(value) -> Optional[str]:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return None


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

