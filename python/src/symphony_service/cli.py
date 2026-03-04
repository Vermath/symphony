"""CLI entrypoint for Symphony service."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .config import ServiceConfig
from .logging_utils import setup_logging
from .orchestrator import Orchestrator
from .status_http import ObservabilityServer
from .workflow import DEFAULT_WORKFLOW_FILE, WorkflowStore, load_workflow

LOGGER = logging.getLogger(__name__)
ACK_FLAG = "--i-understand-that-this-will-be-running-without-the-usual-guardrails"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony",
        description="Symphony orchestration service (Python implementation)",
    )
    parser.add_argument("workflow_path", nargs="?", default=DEFAULT_WORKFLOW_FILE)
    parser.add_argument("--logs-root", default="log", help="Directory to write service logs")
    parser.add_argument("--host", default=None, help="HTTP bind host override")
    parser.add_argument("--port", type=int, default=None, help="Enable HTTP dashboard/API on this port")
    parser.add_argument(
        ACK_FLAG,
        action="store_true",
        dest="acknowledged",
        help="Acknowledge unsafe unattended operation posture",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.acknowledged:
        print(_acknowledgement_banner(), file=sys.stderr)
        raise SystemExit(1)

    workflow_path = Path(args.workflow_path).expanduser().resolve()
    if not workflow_path.is_file():
        print(f"Workflow file not found: {workflow_path}", file=sys.stderr)
        raise SystemExit(1)

    log_file = setup_logging(Path(args.logs_root).expanduser().resolve())
    LOGGER.info("Logging initialized", extra={"component": "cli", "event": str(log_file)})

    try:
        startup_workflow = load_workflow(workflow_path)
        startup_config = ServiceConfig.from_workflow(startup_workflow)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load workflow: {exc}", file=sys.stderr)
        raise SystemExit(1)

    store = WorkflowStore(workflow_path)
    orchestrator = Orchestrator(store)

    error_holder: list[BaseException] = []

    def run_orchestrator() -> None:
        try:
            orchestrator.start()
        except BaseException as exc:  # noqa: BLE001
            error_holder.append(exc)
            LOGGER.exception("Orchestrator crashed", extra={"component": "cli", "error": str(exc)})

    orchestrator_thread = threading.Thread(target=run_orchestrator, name="symphony-orchestrator", daemon=True)
    orchestrator_thread.start()

    server = None
    host = args.host or startup_config.server.host
    port = args.port if args.port is not None else startup_config.server.port
    if port is not None:
        # Host/port CLI overrides workflow values for this run.
        # The server can still be enabled by workflow config when --port is not passed.
        server = ObservabilityServer(orchestrator=orchestrator, host=host, port=port)
        bound_port = server.start()
        if bound_port is not None:
            LOGGER.info("HTTP server bound", extra={"component": "cli", "event": f"{host}:{bound_port}"})

    try:
        while orchestrator_thread.is_alive():
            if error_holder:
                raise error_holder[0]
            time.sleep(0.25)
    except KeyboardInterrupt:
        LOGGER.info("Shutting down on interrupt", extra={"component": "cli"})
    finally:
        orchestrator.stop()
        if server:
            server.stop()
        orchestrator_thread.join(timeout=5)

    if error_holder:
        raise SystemExit(1)


def _acknowledgement_banner() -> str:
    lines = [
        "This Symphony implementation is a low key engineering preview.",
        "Codex will run without any guardrails.",
        "This Python implementation is not a supported product and is presented as-is.",
        f"To proceed, pass {ACK_FLAG}.",
    ]
    width = max(len(line) for line in lines)
    border = "─" * (width + 2)
    content = ["╭" + border + "╮", "│ " + (" " * width) + " │"]
    for line in lines:
        content.append("│ " + line.ljust(width) + " │")
    content += ["│ " + (" " * width) + " │", "╰" + border + "╯"]
    return "\n".join(content)
