# Phase 6 — Progress & Observability

Extends `agent.py`, `cli.py`. Stream token/cost metrics; periodic diff of `src/` vs base commit; spinners and progress bar; `agent-build history <task-id>`.

This phase focuses on improving user experience and observability during long-running task executions.

## Features

- **Live metrics:** Stream token/cost metrics from agent execution
- **Progress feedback:** Periodic diff of `src/` vs base commit; spinners and progress bar
- **History command:** `agent-build history <task-id>` to view past execution attempts

## Scope

- Modifies `agent.py` for event emission of metrics
- Extends `cli.py` for progress rendering and history command
- No changes to core task_run.py orchestration logic
