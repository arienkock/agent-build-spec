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

- `get_latest(task_id) -> Optional[ResultRecord]` — raises `ResultsStoreError` on malformed JSON
- `list_task_ids_in_store() -> set[str]` — all task IDs referenced by any record file
  (used for discrepancy check; parses both latest and archived filenames)
- `check_consistency() -> list[str]` — returns task IDs that have archived records but no latest record (atomic archive failure detection)
- Results dir absent → all `get_latest` calls return `None`; `check_consistency()` returns `[]`

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
0. **Consistency check** — any task ID has archived records but no latest record (atomic archive failure) → ERROR
1. **Discrepancy check** — any record file references a task ID not in `project.tasks` → ERROR; catch `ResultsStoreError` from any `get_latest()` call and convert to ERROR
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
- Parse the **last non-empty line** of output as `{"status": "PASS"|"FAIL", "reasoning": "..."}`
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
- **[HIGH]** The discrepancy check and consistency check (`check_consistency()`) are **mandatory prerequisites** even for explicit targeting. The spec states "The system MUST perform a discrepancy check before resume point logic" with no exception. If either check fails (ERROR state), abort immediately — do not proceed to the confirmation prompt.
- Require explicit user confirmation
- Write `skipped` records for any intermediate tasks that have no latest record (between the current resume point and the target), following the normal archiving procedure
- Leave intermediate tasks that already have a latest record untouched
- Then proceed with normal Task Run for the target task

Add an integration test: `agent-build run <task-id>` with a stale record for a deleted task ID → discrepancy check aborts before confirmation prompt is shown.

---

## Edge Cases & Risk Notes

### CRITICAL

**Task ordering must be explicitly lexicographic on task IDs.**
The resume algorithm relies on "highest-ordered task that has a latest record" and "all tasks before last_task". The spec allows alphanumeric prefixes including formats like `001b-setup-extra` and `01.1-init` — it SHOULD (not MUST) use them; it does NOT restrict to purely zero-padded numeric. Therefore `project.py` **MUST NOT** reject valid spec-compliant alphanumeric IDs. Instead: sort all task IDs by lexicographic order of their full directory name string, and emit a WARNING (not an error) if any task directory name has no leading alphanumeric prefix at all (e.g. bare names like `setup` with no numeric component), since these are likely to produce unintended ordering. Never silently sort by discovery order.

**Shell injection via task file content in agent_command.**
`{prompt}` is substituted into `agent_command` which is executed as a shell command. Task file content read from disk becomes part of that substitution. Pass the prompt via `stdin` or a temp file, or use `subprocess` with an argument list (never `shell=True`). Do not interpolate file content directly into the command string.

**Stale lock file: OS PID reuse.**
A PID recorded in the lock file may be reused by an unrelated process after the original agent-build crashes. Checking `psutil.pid_exists(pid)` is not sufficient — also check the process name/cmdline matches `agent-build`. If it cannot be verified, refuse to proceed and require manual lock removal rather than auto-removing it.

### HIGH

**Malformed or partial result record JSON.**
A crash during an atomic write (after temp-file creation, before rename) leaves only the temp file; the latest record is intact. But a crash during the rename on some filesystems can corrupt the target. `ResultsStore.get_latest()` must catch `json.JSONDecodeError` and **raise a `ResultsStoreError`** (a dedicated exception class defined in `results.py`) rather than returning `None` or propagating a bare `JSONDecodeError`. `resume.py` must catch `ResultsStoreError` when calling `get_latest()` and immediately return `ResumePoint(kind=ERROR, message=...)`, so the user knows manual intervention is needed. The `get_latest() -> Optional[ResultRecord]` signature remains unchanged; the error path uses exceptions, not a sentinel return value.

**Atomic archive failure leaves no latest record.**
`write()` archives the existing latest record first, then writes the new one. If the process dies between those two steps, there is no latest record but an archived one exists. On next load, `get_latest()` returns `None` for that task while archived records exist — this is an inconsistent state. `ResultsStore` should detect this via a new method **`check_consistency() -> list[str]`** that returns the list of task IDs with archived records but no latest record. `resume.py` **MUST** call `results_store.check_consistency()` as an explicit pre-step (before the discrepancy check) and return `ResumePoint(kind=ERROR, message=...)` if any are found. This pre-step must be documented in the `resume.py` algorithm: insert as new **step 0** before the existing discrepancy check.

**Verification output: last non-empty line, not last line.**
"Parse last line of output" must strip trailing newlines/whitespace and find the last non-empty line. If the verification agent produces no parseable JSON (empty output, all whitespace, or exits non-zero), treat as FAIL with a synthetic reasoning string — never propagate a parse exception as an unhandled crash.

**Verifications must only run after exit-code-0 agent completion.**
The plan says "after agent completes: run verifications" — clarify: verifications run only when the agent exits with code 0. A non-zero exit code skips verification and goes directly to the retry/fail path. Otherwise verification results are meaningless against a broken workspace.

**max_retries exhausted must produce a `failed` record and clean exit.**
When the shared retry counter reaches zero (across timeout retries and verification-failure retries), `task_run.py` must write a `failed` result record, release the lock, and exit with a non-zero status code. It must not leave the run in a `running` state. **`src/` MUST be left as-is** — including `.agent-context/` — because the spec mandates that on timeout or failure the workspace contents are preserved so the user can inspect partial state and trigger an explicit additional run. The workspace prep step on the next run is designed to detect an existing `.agent-context/` and prompt for confirmation before overwriting it.

**Rollback: validate `previousResults` chain before starting.**
Before touching any files, `rollback.py` must verify that the file named in `previousResults` actually exists in the results directory. If the chain is broken (file missing or itself malformed), abort with an error — do not partially restore src/ and leave results in an inconsistent state.

**results/ directory must be created by `write()` if absent.**
Phase 1 states "results dir absent → all get_latest calls return None". Phase 2 adds `write()`. `write()` must `mkdir(parents=True, exist_ok=True)` before writing, otherwise the first write to a fresh project fails with `FileNotFoundError`.

**Agent and verification subprocesses must be invoked with `cwd=src/`.**
The spec states "the agent operates within the workspace." The constructed prompt references `.agent-context/task/TASK.md`, `.agent-context/global/GLOBAL.md`, and `.agent-context/verifications/` as relative paths. If the subprocess is launched from the project root, none of these paths resolve and the agent cannot find its instructions. Both `agent.py` and `verification.py` **MUST** set `cwd=<project_root>/src/` when invoking the subprocess. Failure to do so will silently produce empty or hallucinated agent output with no error — a very difficult bug to diagnose.

**`project.py` must abort if a task directory is missing `TASK.md`.**
The spec states: "if a task directory exists without a `TASK.md`, the system MUST abort with an error." `project.py` must check for the presence of `TASK.md` inside each discovered task subdirectory when loading the project, and raise a `ProjectError` immediately rather than returning a `Task` with no entrypoint. Discovering this at validation time (not at agent invocation time) prevents a confusing failure mid-run.

**`project.py` must abort if `tasks/` contains no task subdirectories.**
The spec states: "If `tasks/` contains no task subdirectories, the system MUST abort with an error." `project.py` must enforce this after scanning the `tasks/` directory. An empty `tasks/` directory is not the same as `tasks/` being absent (which is separately checked in preflight); both conditions must be caught and reported with distinct error messages.

---

---

## Testing

All tests use `pytest`. Filesystem fixtures use `tmp_path`. No mocking of subprocess except where explicitly noted. A fake agent subprocess is used for integration tests — it writes to `src/` and exits with a configurable exit code.

### Unit Tests

#### `types.py`
- `TaskRunStatus` enum values match JSON string literals exactly
- `ResultRecord` serializes to camelCase JSON; `skipped` records include only `status` and `previousResults`

#### `project.py`
- **[CRITICAL]** Lexicographic ordering: tasks `001-a`, `002-b`, `001b-extra` sort as `001-a`, `001b-extra`, `002-b`
- **[CRITICAL]** Alphanumeric IDs `01.1-init`, `001b-setup-extra` accepted (not rejected)
- **[HIGH]** Missing `TASK.md` in any task directory raises `ProjectError`
- **[HIGH]** Empty `tasks/` directory raises `ProjectError` with a message distinct from "tasks/ absent"
- Warning (not error) emitted when a task directory name has no leading alphanumeric prefix
- `global/` absent → project loads without error, `project.global_path` is `None`

#### `results.py`
- **[HIGH]** `get_latest()` raises `ResultsStoreError` (not `JSONDecodeError`, not `None`) when JSON is malformed
- **[HIGH]** `check_consistency()` returns task IDs that have archived records but no latest record
- `check_consistency()` returns `[]` when results dir is absent
- `get_latest()` returns `None` when results dir is absent
- `get_latest()` returns `None` when no file for that task ID exists
- **[HIGH]** `write()` creates results dir if absent (`mkdir` before write, no `FileNotFoundError`)
- `write()` on a fresh task: writes `results-<id>.json` with `previousResults: null`
- `write()` on existing latest: archives to `results-<id>--run-1.json`, writes new latest with `previousResults` pointing to archived filename
- `next_archive_order()` returns 1 when no archives exist; returns `max+1` when archives have gaps (e.g. only `run-3` exists → returns 4)
- Atomic write: temp file renamed; existing latest intact if `os.rename` raises mid-write (simulate via mock)
- `list_task_ids_in_store()` parses both latest and archived filenames correctly

#### `resume.py`
- **[HIGH]** `check_consistency()` non-empty → `ResumePoint(kind=ERROR)` before any other step
- **[HIGH]** `get_latest()` raises `ResultsStoreError` → `ResumePoint(kind=ERROR)`
- **[CRITICAL]** Record file references task ID not in `project.tasks` → ERROR
- No records → READY pointing to first task
- Single `running` task → NEEDS_CONFIRMATION
- Two `running` tasks → ERROR
- Last task `failed` → READY (re-run last task)
- All tasks `completed` with more tasks remaining → READY (first unrecorded task)
- All tasks `completed`, no further tasks → COMPLETE
- `skipped` treated as success: `[completed, skipped, <no record>]` → READY (third task)
- Gap in records: tasks 1 and 3 `completed`, task 2 has no record → ERROR
- Non-success intermediate: task 1 `completed`, task 2 `failed`, task 3 `completed` → ERROR
- Task ordering uses lexicographic order (not insertion order)

#### `config.py`
- Missing config file → all defaults applied
- Partial config file → missing fields use defaults, present fields override
- `{model}` substitution happens at invocation time, not at load time
- Unknown fields in config do not raise (forward compatibility)

#### `preflight.py`
- **[CRITICAL]** Lock file with PID of a running `agent-build` process → abort with error
- **[CRITICAL]** Lock file with PID of a running unrelated process → refuse and require manual removal (not auto-remove)
- **[CRITICAL]** Lock file with a non-existent PID → treat as stale, remove, proceed with informational message
- No lock file → proceed normally, lock file created
- Non-git directory → abort with error
- `tasks/` absent → abort with error
- `verifications/` absent → abort with error
- Uncommitted changes → requires user confirmation; if confirmed, proceeds; if denied, aborts
- Untracked files → requires user confirmation (same path as uncommitted changes)
- Clean working tree → proceeds without confirmation

#### `workspace.py`
- Task dir contents copied to `src/.agent-context/task/`
- `global/` contents copied to `src/.agent-context/global/` when present
- `global/` absent → `src/.agent-context/global/` not created, no error
- `verifications/` contents copied to `src/.agent-context/verifications/`
- Existing `src/.agent-context/` → requires user confirmation; if confirmed, deleted and recreated; if denied, aborts

#### `verification.py`
- **[HIGH]** Last non-empty line parsed as JSON: output with trailing blank lines → correct JSON extracted
- **[HIGH]** Empty output → FAIL with synthetic reasoning string (no unhandled exception)
- **[HIGH]** All-whitespace output → FAIL with synthetic reasoning string
- **[HIGH]** Non-zero exit code from verification agent → FAIL with synthetic reasoning string
- Last non-empty line is not valid JSON → FAIL with synthetic reasoning (no `json.JSONDecodeError` propagated)
- Valid PASS response → `("PASS", reasoning)` returned
- Valid FAIL response → `("FAIL", reasoning)` returned
- **[HIGH]** Subprocess invoked with `cwd=<project_root>/src/` (verified via mock capturing kwargs)
- Verification prompt includes: verification file content + task reference + structured response instruction appended

#### `agent.py`
- **[CRITICAL]** Prompt passed via stdin, never interpolated into command string (verify with mock that stdin receives prompt, argv does not)
- **[HIGH]** Subprocess invoked with `cwd=<project_root>/src/` (verified via mock)
- `AgentStarted` event emitted before subprocess starts
- `AgentOutput` events emitted per output chunk
- `AgentCompleted` event emitted with correct `exit_code`
- `AgentTimedOut` event emitted when process exceeds `agent_timeout_seconds`; process killed (not left as orphan)
- Non-zero exit code: `AgentCompleted` emitted (not swallowed)

#### `task_run.py` — retry logic
- **[HIGH]** `max_retries` exhausted → `failed` record written, lock released, process exits non-zero; `src/` (including `.agent-context/`) is left as-is per spec
- **[HIGH]** After `max_retries` exhausted, status is `failed` (not `running`)
- Timeout retry uses identical prompt (not modified)
- Verification failure retry appends only the most recent failure reasoning (not accumulated)
- Retry counter shared between timeout retries and verification-failure retries
- Non-zero agent exit code → no retry, immediate fail, no verification executed

#### `rollback.py`
- **[HIGH]** `previousResults` file referenced but missing → abort before any file changes
- **[HIGH]** `previousResults` file malformed JSON → abort before any file changes
- Latest record status `skipped` → abort with error
- Base commit not in git history → abort with error
- Uncommitted changes present → abort with error
- Untracked files present → abort with error
- Happy path: `src/` restored to base commit, latest record deleted, archived record promoted to latest, new commit created
- Only `src/` restored; `tasks/`, `verifications/`, other `results/` files untouched

#### `results.py` — commit staging
- Commit stages only files within `src/` and `results/`
- No changes in both `src/` and `results/` → abort with error

---

### Integration Tests

These use a real temporary git repo and a fake agent subprocess (writes a file to `src/`, exits with a configurable code).

#### Happy path (Phase 1–2)
- Full run from first task: resume point → preflight → workspace copy → `running` record written before agent → `completed` record written after → `.agent-context/` removed → commit staged to `src/` and `results/` only

#### Resume after failure
- Project with 3 tasks; task 2 has a `failed` latest record → resume point is task 2 → re-executes task 2

#### All tasks complete
- All tasks `completed` → `agent-build status` shows COMPLETE; `agent-build run` reports complete, no execution

#### Discrepancy check
- `results/` contains a record for task `999-deleted` not in `tasks/` → `agent-build run` aborts with ERROR, no side effects

#### Consistency check (atomic archive failure simulation)
- Manually create `results-001-setup--run-1.json` with no corresponding `results-001-setup.json` → `agent-build run` aborts with ERROR

#### Lock file contention
- Lock file with non-existent PID → treated as stale, removed, run proceeds
- Lock file with PID of unrelated running process → aborts, requires manual removal

#### Verification flow (Phase 4)
- Agent exits 0, first verification FAILs → reasoning appended to prompt → agent retried
- Agent exits 0, all verifications PASS → `completed` record written
- Agent exits non-zero → verifications skipped, `failed` record written

#### max_retries exhausted (Phase 4)
- `max_retries=1`; agent exits 0 but verification always FAILs → after 1 retry, `failed` record written, non-zero exit code

#### Rollback (Phase 5)
- Run task 1 (commits), then rollback: `src/` reverted, new commit created, latest record deleted, archived record promoted
- Rollback with `previousResults: null`: latest record deleted, no archive promoted
- Rollback blocked by uncommitted changes

#### Explicit task targeting
- `agent-build run 003-feature` with tasks 1 and 2 having no records: writes `skipped` records for 1 and 2 (with archiving), then runs 3

#### Ordering edge cases (CRITICAL)
- Tasks `001-a`, `001b-extra`, `002-b` all accepted and sorted lexicographically; resume logic uses that order

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
