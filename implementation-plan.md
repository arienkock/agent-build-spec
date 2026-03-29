# Implementation Plan: Agent Build Spec

## Language & Stack

- **Python 3.11+** with full type hints throughout
- **click** for CLI
- CLI command: `agent-build`
- Python package: `agent_build`

## Module Layout

```
agent_build/
  __init__.py
  types.py         — shared dataclasses/enums (Task, ResultRecord, ResumePoint, etc.)
  config.py        — Config dataclass, loaded from agent-build.config.json
  project.py       — Project: load & validate filesystem structure
  results.py       — ResultsStore: read/write/archive result records
  resume.py        — determine_resume_point(): pure logic over tasks + records
  events.py        — typed EventEmitter + event catalog          [Phase 3]
  preflight.py     — git clean check, lock file, dir existence   [Phase 2]
  workspace.py     — copy context into src/.agent-context/       [Phase 2]
  agent.py         — invoke Claude subprocess, emit events       [Phase 3]
  verification.py  — invoke verification agent, parse JSON       [Phase 4]
  task_run.py      — orchestrate full cycle                      [Phase 2+]
  rollback.py      — restore src/ to base commit                 [Phase 5]
  cli.py           — entry point: commands, output rendering
pyproject.toml
```

Each phase adds modules and extends `task_run.py` and `cli.py` — it does **not** rewrite
earlier modules. `types.py`, `project.py`, `results.py`, and `resume.py` are written once
in Phase 1 and remain stable.

---

## Phase 1 — Core structure + Resume Point (CLI-validatable)

**New modules:** `types.py`, `config.py`, `project.py`, `results.py`, `resume.py`, `cli.py`

**What it does:**
- Loads and validates the project structure from disk (tasks/, verifications/, global/)
- Reads existing result records from results/
- Determines the unambiguous resume point (or errors/confirms as required)
- Performs the discrepancy check (records referencing non-existent task IDs)

**CLI commands:**
- `agent-build status` — table of all tasks with their latest result status and run count
- `agent-build run` — prints the resume point; task execution stubbed as "not yet implemented"

**Validates:** project parsing, resume point logic, ambiguity detection, discrepancy check.

### types.py

Key types:
```python
class TaskRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass(frozen=True)
class Task:
    id: str
    path: Path

@dataclass(frozen=True)
class VerificationFile:
    id: str
    path: Path

@dataclass
class ResultRecord:
    status: TaskRunStatus
    previous_results: Optional[str]      # filename of prior archived record, or None
    base_commit: Optional[str] = None
    start_time: Optional[str] = None     # ISO 8601
    end_time: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

class ResumePointKind(str, Enum):
    READY               = "ready"
    COMPLETE            = "complete"
    NEEDS_CONFIRMATION  = "needs_confirmation"   # running task found
    ERROR               = "error"                # ambiguous state

@dataclass
class ResumePoint:
    kind: ResumePointKind
    task: Optional[Task] = None
    message: str = ""
```

### results.py — ResultsStore

- `get_latest(task_id) -> Optional[ResultRecord]`
- `list_task_ids_in_store() -> set[str]` — all task IDs referenced by any record file
  (used for discrepancy check; parses both latest and archived filenames)
- Results dir absent → all `get_latest` calls return `None` (no error)

Record filenames:
- Latest:   `results-<Task ID>.json`
- Archived: `results-<Task ID>--run-<Order>.json`

JSON field names (camelCase to match spec examples):
```json
{
  "status": "completed",
  "previousResults": "results-001-setup--run-1.json",
  "baseCommit": "abc123",
  "startTime": "2026-01-01T10:00:00Z",
  "endTime": "2026-01-01T10:05:00Z",
  "inputTokens": 5000,
  "outputTokens": 1200
}
```

Skipped records contain only `status` and `previousResults`.

### resume.py — determine_resume_point()

Algorithm (in order):
1. **Discrepancy check** — any record file references a task ID not in `project.tasks` → ERROR
2. **Multiple running tasks** → ERROR
3. **No records at all** → READY (first task)
4. Find `last_task`: highest-ordered task that has a latest record
5. **Check all tasks before `last_task`** are successful (COMPLETED or SKIPPED):
   - Any gap (no record) → ERROR
   - Any non-success record → ERROR
6. **Evaluate `last_task`'s status:**
   - `running` → NEEDS_CONFIRMATION
   - `failed` → READY (re-run `last_task`)
   - `completed` / `skipped`:
     - Further tasks exist without records → READY (first unrecorded task)
     - No further tasks → COMPLETE

Note: `skipped` is treated as equivalent to `completed` throughout.

### config.py — Config

Loaded from `agent-build.config.json` at project root; falls back to defaults if absent.

Fields and defaults:
```json
{
  "agent_command": "claude --print --dangerously-skip-permissions --model {model} {prompt}",
  "model": "claude-sonnet-4-6",
  "agent_timeout_seconds": 600,
  "verification_timeout_seconds": 120,
  "max_retries": 3
}
```

`{model}` and `{prompt}` are substituted at invocation time.

---

## Phase 2 — Preflight + Workspace + Results writing + Commits

**New modules:** `preflight.py`, `workspace.py`
**Extended:** `results.py` (write/archive), `task_run.py` (skeleton run), `cli.py`

**What it does:**
- Preflight: verify git repo, required dirs, clean working tree (with confirmation),
  acquire lock file (detect stale by PID check)
- Workspace: copy task/, global/, verifications/ into src/.agent-context/;
  detect and confirm overwrite of existing context dir
- Write `running` record before agent stub; write `completed` record after
- Archive existing latest record before writing new one (atomic: write temp → rename)
- Remove src/.agent-context/ before committing
- Create git commit staging only src/ and results/
- task_run.py stub: preflight → workspace → write running record → (no agent yet, sleep 1s) →
  write completed record → cleanup → commit

**New ResultsStore methods:**
- `write(task_id, record)` — atomic write (temp file → rename), archives existing latest first
- `next_archive_order(task_id) -> int` — max existing archived order + 1

**Validates:** git integration, lock file lifecycle, context copy, atomic record writes,
archiving chain, commit staging scope.

---

## Phase 3 — Real Agent Invocation

**New modules:** `events.py`, `agent.py`
**Extended:** `task_run.py`, `cli.py` (live progress)

**What it does:**
- Invoke `agent_command` as a non-interactive subprocess (no TTY, captured STDIO)
- Emit typed events: `AgentStarted`, `AgentOutput`, `AgentCompleted`, `AgentTimedOut`
- task_run.py subscribes: result recorder updates record on each event
- On timeout: retry with identical prompt (agent inspects workspace and continues)
- On non-zero exit code: no retry, record as failed
- Live progress: periodically report net lines added/removed in src/ vs base commit

**events.py:**
```python
@dataclass
class AgentStarted: pass
@dataclass
class AgentOutput:
    chunk: str
@dataclass
class AgentCompleted:
    exit_code: int
@dataclass
class AgentTimedOut: pass
```

**Validates:** subprocess management, timeout/retry behavior, prompt construction,
partial workspace state preserved on failure.

---

## Phase 4 — Verifications + Retry Loop

**New modules:** `verification.py`
**Extended:** `task_run.py`

**What it does:**
- After agent completes: run verifications in lexicographical order
- Each verification: invoke agent with verification file content + task reference +
  structured response instruction appended
- Parse last line of output as `{"status": "PASS"|"FAIL", "reasoning": "..."}`
- First FAIL halts further verifications; append reasoning to original prompt; retry agent
- On retry: only the most recent failure reasoning is appended (not accumulated)
- All retries (timeout + verification failure) draw from shared `max_retries` counter

**Validates:** full happy-path end-to-end, retry prompt construction, retry limit enforcement.

---

## Phase 5 — Rollback

**New modules:** `rollback.py`
**Extended:** `cli.py`

**What it does:**
- `agent-build rollback` command
- Guards: no uncommitted changes; latest record must not be `skipped`; base commit must
  exist in git history
- Restores src/ to the base commit (only src/, other dirs untouched)
- Deletes latest results record
- Restores previous archived record (via `previousResults` chain) as new latest
- Creates a new commit (history is append-only, no rewrites)

**Validates:** rollback guards, src/ isolation, previousResults chain, commit creation.

---

## Phase 6 — Progress & Observability

**Extended:** `agent.py`, `cli.py`

**What it does:**
- Stream live token/cost metrics if the agent exposes them via output
- Periodic fallback: diff src/ vs base commit, report net lines changed
- Richer CLI output: spinners, task progress bar, cost summary at end
- `agent-build history <task-id>` — list all archived records for a task

---

## Explicit Task Targeting (cross-cutting, add in Phase 2 or 3)

`agent-build run <task-id>` — run a specific task regardless of resume point:
- Require explicit user confirmation
- Write `skipped` records for any intermediate tasks that have no latest record
- Leave intermediate tasks that already have a latest record untouched
- Then proceed with normal Task Run for the target task

---

## Key Design Principles

1. `task_run.py` is the composition root — it wires together preflight, workspace,
   agent, verification, and result recording. Each phase adds to it without changing
   the interface other modules expose.
2. All state mutation (records, workspace, commits) goes through dedicated modules —
   `task_run.py` orchestrates but does not directly touch the filesystem.
3. `resume.py` is pure — it takes `Project` and `ResultsStore` and returns a
   `ResumePoint`. No side effects, fully testable.
4. The EventEmitter (Phase 3) is the only shared mutable object within a task run;
   all components that need to react to agent progress subscribe rather than polling.
