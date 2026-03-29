# Implementation Plan: Agent Build Spec

## Stack

Python 3.11+, full type hints, `click` CLI, command: `agent-build`, package: `agent_build`.

## Module Layout

```
agent_build/
  types.py         — Task, ResultRecord, ResumePoint dataclasses/enums
  config.py        — Config dataclass from agent-build.config.json
  project.py       — load & validate filesystem structure
  results.py       — ResultsStore: read/write/archive result records
  resume.py        — determine_resume_point(): pure logic
  preflight.py     — git clean check, lock file, dir existence   [Phase 2]
  workspace.py     — copy context into src/.agent-context/       [Phase 2]
  events.py        — typed EventEmitter + event catalog          [Phase 3]
  agent.py         — invoke Claude subprocess, emit events       [Phase 3]
  verification.py  — invoke verification agent, parse JSON       [Phase 4]
  task_run.py      — orchestrate full cycle                      [Phase 2+]
  rollback.py      — restore src/ to base commit                 [Phase 5]
  cli.py           — entry point: commands, output rendering
```

`types.py`, `project.py`, `results.py`, `resume.py` are written in Phase 1 and remain stable. Each phase extends `task_run.py` and `cli.py`.

---

## Phase 1 — Core Structure + Resume Point

**CLI:** `agent-build status` (task table), `agent-build run` (prints resume point; execution stubbed).

### Key Types

```python
class TaskRunStatus(str, Enum):
    RUNNING = "running"; COMPLETED = "completed"; FAILED = "failed"; SKIPPED = "skipped"

@dataclass(frozen=True)
class Task:
    id: str; path: Path

@dataclass
class ResultRecord:
    status: TaskRunStatus
    previous_results: Optional[str]   # archived filename or None
    base_commit: Optional[str] = None
    start_time: Optional[str] = None  # ISO 8601
    end_time: Optional[str] = None
    cpu_user_time: Optional[float] = None
    cpu_system_time: Optional[float] = None
    io_time: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

class ResumePointKind(str, Enum):
    READY = "ready"; COMPLETE = "complete"
    NEEDS_CONFIRMATION = "needs_confirmation"  # running task found
    ERROR = "error"                            # ambiguous state
```

### results.py

- Filenames: latest `results-<id>.json`, archived `results-<id>--run-<N>.json`
- JSON uses camelCase (`previousResults`, `baseCommit`, `startTime`, etc.)
- Skipped records contain only `status` and `previousResults`
- `get_latest(task_id)` raises `ResultsStoreError` on malformed JSON; returns `None` if non-existent
- `check_consistency()` returns task IDs with archived records but no latest record
- `write()` creates results dir if absent; archives existing latest atomically (temp → rename) before writing new
- Archive numbering: `max_existing_archived_order + 1` (handles gaps from manual deletion)

### resume.py Algorithm

0. **Discrepancy check (MUST run before resume logic):** Scan all files in `results/` (both latest `results-<id>.json` and archived `results-<id>--run-<N>.json`). Extract task IDs from filenames. If any task ID does not correspond to a task directory on disk → ERROR. This must check all results files — including archives — because the spec says "any results record".
1. `check_consistency()` non-empty (archived record exists without a corresponding latest record) → ERROR; `ResultsStoreError` from `get_latest()` → ERROR
2. Multiple `running` tasks → ERROR
3. No records → READY (first task)
4. Find `last_task`: highest-ordered task with a latest record
5. All tasks before `last_task` must be `completed` or `skipped`; any other status or absent record → ERROR
6. Evaluate `last_task`: `running` → NEEDS_CONFIRMATION; `failed` → READY (re-run); `completed`/`skipped` → READY (next unrecorded task) or COMPLETE (none remain)

### config.py Defaults

```json
{
  "agent_command": "claude --print --dangerously-skip-permissions --model {model}",
  "model": "claude-sonnet-4-6",
  "agent_timeout_seconds": 600,
  "verification_timeout_seconds": 120,
  "max_retries": 3
}
```

Prompt always passed via stdin, never interpolated into `agent_command`. Command split to argv list, `shell=False`. Missing fields use per-field defaults; unknown fields ignored; zero/negative timeout → validation error. **`{model}` substitution must use keyword-only `.format(model=model)` (HIGH):** positional or extra braces in user-supplied `agent_command` (e.g. `{0}`, `{foo}`) would cause a confusing `KeyError`/`IndexError` at launch time rather than a config validation error. Use `.format(model=model)` or `str.replace('{model}', model)` to restrict substitution to the known key only.

---

## Phase 2 — Preflight + Workspace + Commits

**Preflight order:**
1. **Startup cleanup (CRITICAL):** Delete any `results/*.tmp` files before the dirty-tree check. These are left by crashed atomic writes and would permanently block the dirty-tree check.
2. Verify git repo, required dirs (`src/`, `tasks/`, `verifications/`), clean working tree (prompt if dirty).
3. **Empty repo check (CRITICAL):** Verify `git rev-parse HEAD` succeeds (at least one commit exists). Without an initial commit, `base_commit` cannot be captured, making rollback impossible and producing a confusing mid-run failure. Abort with a clear error if the repo has no commits.
4. **Detached HEAD check (HIGH):** Verify `git symbolic-ref HEAD` succeeds (repo is on a named branch). Committing on a detached HEAD creates an unreachable commit, silently losing agent work. Abort with a clear error if HEAD is detached.
5. Acquire lock at `<project_root>/.agent-build.lock` via `O_CREAT | O_EXCL | O_WRONLY`. Stale lock: check PID exists AND cmdline matches `agent-build`; refuse if unverifiable. Non-parseable, corrupted, or empty lock file content → refuse with a clear error naming the lock file path.

**Workspace:** copy task dir → `src/.agent-context/task/`, `global/` → `src/.agent-context/global/` (skip if absent), `verifications/` → `src/.agent-context/verifications/`. Confirm before overwriting existing `.agent-context/`; if confirmed, delete then recopy.

**Gitignore guard (CRITICAL):** Before copying, ensure `.agent-context` is in `src/.gitignore` (append/create if needed). Without this, failed runs leave untracked files that block subsequent preflight checks and rollback.

**Records:** write `running` record (with `base_commit`) before agent; write `completed` after. Remove `src/.agent-context/` before committing (success path only). Commit stages only `src/` and `results/`; abort cleanly if no changes in either.

**Task ordering:** lexicographic on full directory name. Accept formats like `001b-setup-extra`, `01.1-init`. Emit WARNING (not error) for names with no leading alphanumeric prefix. Abort if any task dir lacks `TASK.md`; use distinct errors for empty `tasks/` vs. absent `tasks/`.

**Explicit task targeting** (`agent-build run <task-id>`): run consistency and discrepancy checks first (abort before confirmation if either fails). After confirmation, write `skipped` records for intermediate tasks with no latest record (do not overwrite existing records); run target task normally. Intermediate tasks that already have a latest record (including `failed` records) are **left untouched** per the spec — aborting on failed intermediates is not permitted. Note: this can result in a history where a failed intermediate precedes a completed later task, which will be ambiguous under normal resume logic; the user is responsible for reconciling this via further explicit targeting or manual intervention.

---

## Phase 3 — Agent Invocation

**Prompt template:**
```
You are a coding agent.

Your task instructions are in `.agent-context/task/TASK.md`. Read them before doing
anything else. The file may reference additional files within `.agent-context/task/`
for progressive disclosure — load them as needed.

[CONDITIONAL — include only when global/GLOBAL.md was copied into the workspace:]
Global instructions that apply to all tasks are in `.agent-context/global/GLOBAL.md`.
Read these before beginning work.

Verification checks that will be run against your output are defined as files in
`.agent-context/verifications/`. You MAY review them in advance to understand what
success looks like.

Complete the task. When you are done, stop. Verifications will be run automatically.
```

- Subprocess: no TTY, captured stdio, `cwd=<root>/src/`, prompt via stdin
- Events: `AgentStarted`, `AgentOutput(chunk)`, `AgentCompleted(exit_code)`, `AgentTimedOut`
- Timeout → retry with identical original prompt (shared `max_retries` counter)
- Non-zero exit → no retry → `failed` record
- `OSError` on launch (binary not found, etc.): catch around `Popen`; write `failed` record, do not retry, do not propagate past `agent.py`
- SIGINT/SIGTERM: kill subprocess, then **call `process.wait()` to reap the zombie before re-raising** (HIGH: without `wait()`, the killed process lingers as a zombie, keeping its PID entry alive and causing stale-lock PID checks to incorrectly see the process as still running); `running` record remains. On next run: NEEDS_CONFIRMATION → **read `base_commit` from existing running record first**, then overwrite with new `running` record using that preserved value, re-run from scratch
- Lock released in `finally` block (including `KeyboardInterrupt`)
- Live metric updates: subscribe to agent events, update `running` record with partial token/timing metrics as they arrive

---

## Phase 4 — Verifications + Retry Loop

- Run only after agent exits 0; non-zero exit → skip verification → fail path
- No verification files → skip entirely, treat as all-pass
- Run in lexicographic order; **halt on first FAIL**; `cwd=<root>/src/`
- **Verification prompt:** verification file content verbatim, then append:
  - `The task instructions are in .agent-context/task/TASK.md. Read them before making your assessment.`
  - `Respond with a single JSON object on the last line of your output. Do not include any text after the JSON object. { "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }`
- Parse **last non-empty line** as `{"status": "PASS"|"FAIL", "reasoning": "..."}`. Empty output, non-JSON, non-zero exit, or timeout → FAIL with synthetic reasoning (never propagate parse exceptions). Timeout uses `verification_timeout_seconds`.
- `OSError` on launch → FAIL with synthetic reasoning; do not propagate
- SIGINT during verification: kill subprocess, re-raise; lock released in outer `finally`; `running` record persists; next run → NEEDS_CONFIRMATION
- **Retry prompt:** `{original_prompt}\n\n---\n\nThe following verification failed. Review the reasoning and correct the issue.\n\nVerification: \`.agent-context/verifications/{id}.md\`\n\nReasoning:\n{reasoning}` where `{id}` is the filename stem (e.g. `01-check-output` for `01-check-output.md`). Only the most recent failure is appended, not accumulated.
- Timeout and verification-failure retries share `max_retries`; exhausted → `failed` record, `src/` left as-is for inspection

---

## Phase 5 — Rollback

Command: `agent-build rollback`

**Guards (checked in order before touching files):**
1. No uncommitted/untracked changes
2. Latest record not `skipped`
3. Base commit in git history
4. If `previousResults` non-null: referenced archive exists and is valid JSON

**Actions:** restore `src/` to base commit; delete latest record; if `previousResults` non-null, **move** (not copy) archive to become new latest record; commit staging `src/` and `results/`.

**Partial failure (HIGH):** If the final `git commit` fails (e.g., pre-commit hook), `src/` and records are already mutated. Surface a clear error: "`src/` has been reset, results records updated, but the rollback commit could not be created." Do not attempt to undo filesystem mutations. This is an acceptable known failure mode.

---

## Phase 6 — Progress & Observability

Extends `agent.py`, `cli.py`. Stream token/cost metrics; periodic diff of `src/` vs base commit; spinners and progress bar; `agent-build history <task-id>`.

---

## Testing

`pytest` with `tmp_path` fixtures. No subprocess mocking except where noted. Integration tests use real temp git repo and fake agent subprocess (configurable exit code).

### Unit Tests

| Module | Key cases |
|---|---|
| `project.py` | Lexicographic sort; missing `TASK.md` → error; empty `tasks/` distinct from absent; task dir with no leading alphanumeric prefix → WARNING emitted (not error); valid formats accepted without warning (`001b-setup-extra`, `01.1-init`) |
| `resume.py` | Discrepancy check first (unknown task ID in latest AND archived filenames → ERROR); consistency check (archived without latest → ERROR); no records → READY (first task); all tasks completed → COMPLETE; single completed task (last in list) → COMPLETE; all skipped → COMPLETE; gap → ERROR; running at last → NEEDS_CONFIRMATION; running not at last → ERROR; failed → READY; completed with remaining → READY (next) |
| `preflight.py` | `O_EXCL` atomicity; stale PID with mismatched cmdline → refuse; absent PID → acquire; corrupted/empty lock file → refuse naming path; `.tmp` files deleted before dirty-tree check; empty repo (no HEAD commit) → abort; detached HEAD → abort |
| `workspace.py` | `.agent-context` added to gitignore; no duplicate append; global absent → skip; existing `.agent-context/` triggers confirm |
| `agent.py` | Prompt via stdin not argv; `cwd=src/`; SIGINT kills subprocess then `wait()` reaps zombie; timeout → kill + `wait()` + event; on resume after SIGINT, `base_commit` read before overwrite; `OSError` → `failed` record, no retry |
| `verification.py` | Last non-empty line parsed; non-zero exit → FAIL with synthetic reasoning; empty/non-JSON/timeout → FAIL; `cwd=src/`; timeout uses `verification_timeout_seconds`; no files → skip; lexicographic order; halt on first FAIL; retry `{id}` is filename stem; `OSError` → FAIL synthetic; SIGINT → kill + re-raise |
| `task_run.py` | `max_retries` exhausted → failed; lock released on exception; global prompt conditional on GLOBAL.md copied; retry prompt format (latest failure only); non-zero → verification skipped; `.agent-context/` removed on success only; commit aborts if no changes; timeout retry uses original prompt; explicit targeting with failed intermediate → intermediate left untouched, target task runs normally; non-existent task ID → abort before side effects; `base_commit` preserved from prior running record; shared counter: one timeout + one verification failure together exhaust `max_retries=2` |
| `config.py` | Missing file → all defaults; zero timeout → validation error; negative timeout → validation error; extra fields ignored; `agent_command` with extra braces (e.g. `{0}`) → validation error or safe substitution, not runtime crash; `agent_command` split produces argv list (shell=False, no injection) |
| `results.py` | Malformed JSON → `ResultsStoreError`; non-existent → `None`; archive numbering with gaps; atomic write; `check_consistency()` detects broken chain; `check_consistency()` returns empty set when all chains intact; `write()` creates dir if absent; skipped record serializes only `status` and `previousResults` fields; JSON uses camelCase keys (`previousResults`, `baseCommit`, `startTime`) |
| `rollback.py` | Missing `previousResults` file → abort before changes; null chain → delete only; skipped → abort; missing base commit → abort; guard order enforced; archive moved not copied; no-op rollback → clear error, no empty commit |

### Integration Tests

- **Happy path:** full cycle with gitignore guard, running/completed records, `.agent-context/` cleanup, commit
- **Resume after failure:** task 2 failed → re-runs task 2; earlier tasks unchanged
- **Discrepancy/consistency checks:** stale record → abort; archived with no latest → abort
- **Verification retry:** FAIL → retry with latest reasoning only; exhausted → `failed`; non-zero exit → fail without verification
- **Agent timeout:** subprocess killed → retry with original prompt; always-timing-out → `failed`
- **Rollback:** `src/` reverted, new commit, record chain restored; blocked by dirty tree; blocked by skipped record; commit failure after mutations → clear error, no silent crash
- **Agent binary not found:** `OSError` caught → `failed` record → lock released
- **SIGINT during verification:** subprocess killed, lock released, `running` record persists → next run NEEDS_CONFIRMATION
- **SIGINT during agent:** running record remains; confirm → re-runs; `base_commit` in new record matches interrupted record
- **Explicit targeting:** intermediate tasks with no record get skipped records; intermediate tasks with existing records (including `failed`) are left untouched; target task runs normally; discrepancy/consistency errors abort before confirmation prompt
- **Startup tmp cleanup:** `.tmp` file in `results/` → deleted at startup → dirty-tree check passes
- **Empty git repo:** repo has no commits → `agent-build run` aborts with clear error before writing any records
- **Detached HEAD:** repo is in detached HEAD state → `agent-build run` aborts with clear error before writing any records
- **Zombie reaping:** agent killed mid-run (SIGINT) → `process.wait()` called → process not listed in zombie state
- **Workspace overwrite:** confirm → delete + recopy; deny → abort
- **Live metrics:** kill mid-run → partial metrics in running record
- **Lock contention:** second concurrent `agent-build run` invocation while first holds lock → clear error, does not block or corrupt
- **Missing config file:** no `agent-build.config.json` present → all defaults applied, run proceeds normally
- **No-change commit:** agent makes no file changes in `src/` and `results/` unchanged → commit aborts with clear error, no empty commit created
