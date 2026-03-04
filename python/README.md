# Symphony Python

This is a full Python implementation of Symphony based on [`../SPEC.md`](../SPEC.md).

Implemented core conformance areas include:

- `WORKFLOW.md` loader with YAML front matter and prompt template body
- Typed config layer with defaults, `$VAR` env resolution, and preflight validation
- Dynamic workflow reload with last-known-good fallback on invalid updates
- Polling orchestrator with single in-memory authoritative state
- Linear tracker adapter (candidate fetch, state refresh, terminal sweep)
- Jira Cloud tracker adapter (JQL poll/state refresh/terminal sweep)
- Optional local `memory` tracker for offline/testing runs
- Workspace manager with path safety checks and lifecycle hooks
- Codex app-server JSON-lines session client (`initialize`, `thread/start`, `turn/start`, stream events)
- Strict template rendering (`issue` + `attempt`) with hard failures on unknown variables/filters
- Retry queue with exponential backoff and continuation retries
- Reconciliation that stops runs on terminal/non-active tracker transitions
- Startup + transition terminal workspace cleanup
- Structured JSON logs with issue/session context
- Optional HTTP dashboard + JSON API (`/`, `/api/v1/state`, `/api/v1/<issue_identifier>`, `/api/v1/refresh`)

## Requirements

- Python `3.10+`
- `codex` CLI available on PATH when using `tracker.kind: linear` or real runs
- `LINEAR_API_KEY` set when using Linear
- `JIRA_API_TOKEN` + `JIRA_EMAIL` set when using Jira

## Setup

```bash
cd python
python -m pip install -e .
```

## Run

```bash
symphony --i-understand-that-this-will-be-running-without-the-usual-guardrails ./WORKFLOW.md
```

Optional flags:

- `--logs-root <path>`: structured log output directory
- `--port <port>`: enable observability HTTP server
- `--host <host>`: HTTP bind host override

## Example Workflows

- Linear-backed: [`WORKFLOW.linear.example.md`](./WORKFLOW.linear.example.md)
- Jira-backed: [`WORKFLOW.jira.example.md`](./WORKFLOW.jira.example.md)
- Offline/testing memory tracker: [`WORKFLOW.memory.example.md`](./WORKFLOW.memory.example.md)

## Tests

```bash
cd python
python -m unittest discover -s tests -v
```
