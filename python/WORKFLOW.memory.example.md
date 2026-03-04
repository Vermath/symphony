---
tracker:
  kind: memory
  memory_file: ./sample_issues.json
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
polling:
  interval_ms: 2000
workspace:
  root: ./workspaces
agent:
  max_concurrent_agents: 2
  max_turns: 5
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
server:
  host: 127.0.0.1
  port: 8080
---

You are working on local issue {{ issue.identifier }}.

Title: {{ issue.title }}
State: {{ issue.state }}
Description: {{ issue.description }}

Complete the work and update issue artifacts in this workspace.

