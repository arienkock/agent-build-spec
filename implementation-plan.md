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
    cpu_user_time: Optional[float] = None   # seconds
    cpu_system_time: Optional[float] = None # seconds
    io_time: Optional[float] = None         # seconds
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
  "cpuUserTime": 12.4,
  "cpuSystemTime": 1.1,
  "ioTime": 0.3,
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
  "agent_command": "claude --print --dangerously-skip-permissions --model {model}",
  "model": "claude-sonnet-4-6",
  "agent_timeout_seconds": 600,
  "verification_timeout_seconds": 120,
  "max_retries": 3
}
```

`{model}` is substituted at invocation time. The prompt is **always passed via stdin** — it is never interpolated into the command string. The command is split into an argument list and executed with `subprocess` and `shell=False`; the prompt is written to the process's stdin after launch. This prevents shell injection via task file content.

---

## Phase 2 — Preflight + Workspace + Results writing + Commits

**New modules:** `preflight.py`, `workspace.py`
**Extended:** `results.py` (write/archive), `task_run.py` (skeleton run), `cli.py`

**What it does:**
- Preflight: verify git repo, required dirs, clean working tree (with confirmation),
  acquire lock file (detect stale by PID check)
- Workspace: copy current task dir contents → `src/.agent-context/task/`,
  `global/` contents → `src/.agent-context/global/` (omit if absent),
  `verifications/` contents → `src/.agent-context/verifications/`;
  detect and confirm overwrite of existing context dir
- Write `running` record before agent stub; write `completed` record after
- Archive existing latest record before writing new one (atomic: write temp → rename)
- Remove src/.agent-context/ before committing
- Abort with error if there are no changes in **either** src/ **or** results/ to stage (i.e. both are empty — nothing to commit)
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
- After agent completes **with exit code 0**: run verifications in lexicographical order;
  a non-zero exit code skips verification entirely and goes directly to the retry/fail path
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
- Guards: no uncommitted changes **and no untracked files**; latest record must not be
  `skipped`; base commit must exist in git history
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

## Edge Cases & Risk Notes

### CRITICAL

**Task ordering must be explicitly lexicographic on task IDs.**
The resume algorithm relies on "highest-ordered task that has a latest record" and "all tasks before last_task". If task IDs are not zero-padded (e.g. `task-9` vs `task-10`), lexicographic sort gives wrong order. The spec must require zero-padded numeric prefixes, and `project.py` must sort by the full ID string and reject non-conforming names at load time.

**Shell injection via task file content in agent_command.**
`{prompt}` is substituted into `agent_command` which is executed as a shell command. Task file content read from disk becomes part of that substitution. Pass the prompt via `stdin` or a temp file, or use `subprocess` with an argument list (never `shell=True`). Do not interpolate file content directly into the command string.

**Stale lock file: OS PID reuse.**
A PID recorded in the lock file may be reused by an unrelated process after the original agent-build crashes. Checking `psutil.pid_exists(pid)` is not sufficient — also check the process name/cmdline matches `agent-build`. If it cannot be verified, refuse to proceed and require manual lock removal rather than auto-removing it.

### HIGH

**Malformed or partial result record JSON.**
A crash during an atomic write (after temp-file creation, before rename) leaves only the temp file; the latest record is intact. But a crash during the rename on some filesystems can corrupt the target. `ResultsStore.get_latest()` must catch `json.JSONDecodeError` and treat it as an ERROR-level resume condition (not silently return `None`), so the user knows manual intervention is needed.

**Atomic archive failure leaves no latest record.**
`write()` archives the existing latest record first, then writes the new one. If the process dies between those two steps, there is no latest record but an archived one exists. On next load, `get_latest()` returns `None` for that task while archived records exist — this is an inconsistent state. `ResultsStore` should detect this (archived records exist but no latest) and surface it as an ERROR resume condition.

**Verification output: last non-empty line, not last line.**
"Parse last line of output" must strip trailing newlines/whitespace and find the last non-empty line. If the verification agent produces no parseable JSON (empty output, all whitespace, or exits non-zero), treat as FAIL with a synthetic reasoning string — never propagate a parse exception as an unhandled crash.

**Verifications must only run after exit-code-0 agent completion.**
The plan says "after agent completes: run verifications" — clarify: verifications run only when the agent exits with code 0. A non-zero exit code skips verification and goes directly to the retry/fail path. Otherwise verification results are meaningless against a broken workspace.

**max_retries exhausted must produce a `failed` record and clean exit.**
When the shared retry counter reaches zero (across timeout retries and verification-failure retries), `task_run.py` must write a `failed` result record, remove `.agent-context/`, release the lock, and exit with a non-zero status code. It must not leave the run in a `running` state.

**Rollback: validate `previousResults` chain before starting.**
Before touching any files, `rollback.py` must verify that the file named in `previousResults` actually exists in the results directory. If the chain is broken (file missing or itself malformed), abort with an error — do not partially restore src/ and leave results in an inconsistent state.

**results/ directory must be created by `write()` if absent.**
Phase 1 states "results dir absent → all get_latest calls return None". Phase 2 adds `write()`. `write()` must `mkdir(parents=True, exist_ok=True)` before writing, otherwise the first write to a fresh project fails with `FileNotFoundError`.

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
