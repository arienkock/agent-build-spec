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
- JSON uses camelCase keys (`previousResults`, `baseCommit`, `startTime`, etc.)
- Skipped records contain only `status` and `previousResults`
- `get_latest(task_id)` raises `ResultsStoreError` on malformed JSON; returns `None` if absent
- `check_consistency()` returns task IDs with an archived record but no latest record
- `write()` creates results dir if absent; archives existing latest atomically (temp → rename) before writing new
- Archive numbering: `max_existing_archived_order + 1` (handles gaps)

### resume.py Algorithm

1. **Discrepancy check (first):** Scan all files in `results/` (latest and archived). Extract task IDs from filenames. Any task ID with no corresponding task directory → ERROR.
2. `check_consistency()` non-empty, or `ResultsStoreError` from `get_latest()` → ERROR
3. Multiple `running` tasks → ERROR
4. No records → READY (first task)
5. Find `last_task`: highest-ordered task with a latest record
6. All tasks before `last_task` must be `completed` or `skipped`; any other status or absent record → ERROR
7. Evaluate `last_task`: `running` → NEEDS_CONFIRMATION; `failed` → READY (re-run); `completed`/`skipped` → READY (next task) or COMPLETE (none remain)

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

- Prompt passed via stdin, never in `agent_command`. Command split to argv list, `shell=False`.
- Missing fields use per-field defaults; unknown fields ignored; zero/negative timeout → validation error.
- **`{model}` substitution:** use `.format(model=model)` (keyword-only). Both positional placeholders like `{0}` (would raise `IndexError` at substitution time) and unknown named keys like `{unknown}` (would raise `KeyError`) must be caught at config validation time — not at runtime. Validation rule: after stripping `{model}`, any remaining `{...}` placeholder → validation error.

---

## Phase 2 — Preflight + Workspace + Commits

**Preflight order:**
1. **Startup cleanup (CRITICAL):** Delete any `results/*.tmp` files before the dirty-tree check (crash artifacts that would permanently block it).
2. Verify git repo, required dirs (`src/`, `tasks/`, `verifications/`), clean working tree (prompt if dirty).
3. **Empty repo check (CRITICAL):** `git rev-parse HEAD` must succeed — no commits means `base_commit` can't be captured and rollback is impossible. Abort with a clear error.
4. **Detached HEAD check (HIGH):** `git symbolic-ref HEAD` must succeed — committing on detached HEAD creates an unreachable commit. Abort with a clear error.
5. Acquire lock at `<project_root>/.agent-build.lock` via `O_CREAT | O_EXCL | O_WRONLY`. Stale lock: check PID exists AND cmdline matches `agent-build`; refuse if unverifiable. Non-parseable or empty lock file → refuse with error naming the lock file path.

**Workspace:** copy task dir → `src/.agent-context/task/`, `global/` → `src/.agent-context/global/` (skip if absent), `verifications/` → `src/.agent-context/verifications/`. Confirm before overwriting existing `.agent-context/`; if confirmed, delete then recopy.

**Gitignore guard (CRITICAL):** Before copying, ensure `.agent-context` is in `src/.gitignore` (append/create if needed). Without this, failed runs leave untracked files that block subsequent preflight checks and rollback.

**Records:** write `running` record (with `base_commit`) before agent; write `completed` after. Remove `src/.agent-context/` before committing (success path only). Commit stages only `src/` and `results/`; abort cleanly if no changes in either.

**Retries do not repeat preflight or workspace prep (CRITICAL):** Preflight checks and workspace preparation run exactly once per task run, before the first agent invocation. All retries (timeout-triggered or verification-failure-triggered) skip both steps and reuse the already-prepared workspace and held lock throughout the retry loop.

**Task ordering:** lexicographic on full directory name. Accept formats like `001b-setup-extra`, `01.1-init`. Emit WARNING (not error) for names with no leading alphanumeric prefix. Abort if any task dir lacks `TASK.md`; use distinct errors for empty `tasks/` vs. absent `tasks/`.

**Explicit task targeting** (`agent-build run <task-id>`): run discrepancy and consistency checks first (abort before confirmation if either fails). After confirmation, write `skipped` records for intermediate tasks with no latest record; do not overwrite existing records of intermediate tasks (including `failed`). The target task itself always runs — this includes re-running a task that already has a successful or skipped record; its existing record is archived normally when the new `running` record is written.

---

## Phase 3 — Agent Invocation

**Prompt template:**
```
You are a coding agent.

Your task instructions are in `.agent-context/task/TASK.md`. Read them before doing
anything else. The file may reference additional files within `.agent-context/task/`
for progressive disclosure — load them as needed.

[CONDITIONAL — only when global/GLOBAL.md was copied:]
Global instructions that apply to all tasks are in `.agent-context/global/GLOBAL.md`.
Read these before beginning work.

Verification checks that will be run against your output are defined as files in
`.agent-context/verifications/`. You MAY review them to understand what success looks like.

Complete the task. When you are done, stop. Verifications will be run automatically.
```

- Subprocess: no TTY, captured stdio, `cwd=<root>/src/`, prompt via stdin
- Events: `AgentStarted`, `AgentOutput(chunk)`, `AgentCompleted(exit_code)`, `AgentTimedOut`
- Timeout → retry with original prompt (shared `max_retries` counter); non-zero exit → no retry → `failed` record
- `OSError` on launch → catch around `Popen`; write `failed` record, no retry, no propagation
- **SIGINT/SIGTERM (HIGH):** A SIGTERM signal handler must be installed (e.g., `signal.signal(signal.SIGTERM, handler)`) that raises an exception (e.g., `SystemExit` or a custom `TerminatedError`). Without this, Python's default SIGTERM behavior terminates the process immediately — skipping `finally` blocks, leaving the subprocess running (zombie risk), and leaking the lock. With the handler installed: kill subprocess, then `process.wait()` to reap zombie before re-raising (HIGH: without `wait()`, stale-lock PID checks may incorrectly see the process as alive). `running` record remains. On next run: NEEDS_CONFIRMATION → read `base_commit` from existing running record before overwriting, then re-run from scratch.
- Lock released in `finally` (including `KeyboardInterrupt` and SIGTERM via installed handler); live metrics update `running` record as events arrive

---

## Phase 4 — Verifications + Retry Loop

- Run only after agent exits 0; non-zero exit → skip verification → fail path
- No verification files → skip entirely, treat as all-pass
- Run in lexicographic order; halt on first FAIL; `cwd=<root>/src/`
- **Verification prompt:** verification file content verbatim, then append:
  - `The task instructions are in .agent-context/task/TASK.md. Read them before making your assessment.`
  - `Respond with a single JSON object on the last line of your output. Do not include any text after the JSON object. { "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }`
- Parse **last non-empty line** as `{"status": "PASS"|"FAIL", "reasoning": "..."}`. Empty output, non-JSON, non-zero exit, or timeout → FAIL with synthetic reasoning (never propagate parse exceptions).
- `OSError` on launch → FAIL with synthetic reasoning; do not propagate
- **SIGINT during verification:** kill subprocess, re-raise; lock released in outer `finally`; `running` record persists; next run → NEEDS_CONFIRMATION
- **Retry prompt:** `{original_prompt}\n\n---\n\nThe following verification failed. Review the reasoning and correct the issue.\n\nVerification: \`.agent-context/verifications/{id}.md\`\n\nReasoning:\n{reasoning}` — `{id}` is the filename stem. Only the most recent failure is appended.
- Timeout and verification-failure retries share `max_retries`; exhausted → `failed` record, `src/` left as-is for inspection

---

## Phase 5 — Rollback

Command: `agent-build rollback`

**Lock acquisition (HIGH):** Acquire the project lock (same `O_EXCL` mechanism as `run`) before any guards or file mutations. Without this, a concurrent `agent-build rollback` and `agent-build run` can corrupt both `src/` and `results/` records simultaneously. Apply the same stale-lock and unverifiable-lock rules as preflight; release in `finally`.

**Guards (checked in order before touching files):**
1. No uncommitted/untracked changes
2. Latest record not `skipped`
3. Base commit in git history
4. If `previousResults` non-null: referenced archive exists and is valid JSON

**Actions:** restore `src/` to base commit; delete latest record; if `previousResults` non-null, **move** (not copy) archive to become new latest record; commit staging `src/` and `results/`.

**Partial failure (HIGH):** If `git commit` fails (e.g., pre-commit hook), `src/` and records are already mutated. Surface a clear error: "`src/` has been reset, results records updated, but the rollback commit could not be created." Do not undo filesystem mutations.

---

## Phase 6 — Progress & Observability

Extends `agent.py`, `cli.py`. Stream token/cost metrics; periodic diff of `src/` vs base commit; spinners and progress bar; `agent-build history <task-id>`.

---

## Testing

`pytest` with `tmp_path` fixtures. No subprocess mocking except where noted. Integration tests use a real temp git repo and a fake agent subprocess (configurable exit code).

### Unit Tests

| Module | Key cases |
|---|---|
| `project.py` | Lexicographic sort; missing `TASK.md` → error; empty `tasks/` distinct from absent; no leading alphanumeric → WARNING only; valid formats (`001b-setup-extra`, `01.1-init`) accepted without warning |
| `resume.py` | Discrepancy check first (unknown task ID in latest AND archived → ERROR); consistency check (archived without latest → ERROR); no records → READY; all completed → COMPLETE; all skipped → COMPLETE; gap → ERROR; running at last → NEEDS_CONFIRMATION; running not at last → ERROR; failed → READY |
| `preflight.py` | `O_EXCL` atomicity; stale PID with mismatched cmdline → refuse; absent PID → acquire; corrupted/empty lock → refuse naming path; `.tmp` deleted before dirty-tree check; empty repo → abort; detached HEAD → abort |
| `workspace.py` | `.agent-context` added to gitignore; no duplicate append; global absent → skip; existing `.agent-context/` triggers confirm |
| `agent.py` | Prompt via stdin not argv; `cwd=src/`; SIGINT kills then `wait()` reaps; timeout → kill + `wait()` + event; resume preserves `base_commit`; `OSError` → `failed`, no retry |
| `verification.py` | Last non-empty line parsed; non-zero → FAIL synthetic; empty/non-JSON/timeout → FAIL; `cwd=src/`; no files → skip; lexicographic halt on first FAIL; retry `{id}` is filename stem; `OSError` → FAIL synthetic; SIGINT → kill + re-raise |
| `task_run.py` | `max_retries` exhausted → failed; lock released on exception; global prompt conditional; retry prompt format (latest failure only); non-zero → verification skipped; `.agent-context/` removed on success only; commit aborts if no changes; explicit targeting: failed intermediate left untouched; explicit targeting: completed target task archives old record and runs; shared counter: one timeout + one verification failure exhaust `max_retries=2`; workspace prep and preflight not repeated on timeout retry |
| `config.py` | Missing file → all defaults; zero/negative timeout → validation error; extra fields ignored; `agent_command` with `{0}` → validation error; `agent_command` with `{unknown}` → validation error; `agent_command` with only `{model}` → valid; argv split for shell=False |
| `results.py` | Malformed JSON → `ResultsStoreError`; non-existent → `None`; archive numbering with gaps; atomic write; consistency detects broken chain; `write()` creates dir if absent; skipped record serializes only `status` and `previousResults`; camelCase keys |
| `rollback.py` | Missing `previousResults` file → abort before changes; null chain → delete only; skipped → abort; missing base commit → abort; guard order enforced; archive moved not copied; no-op → clear error, no empty commit; lock already held → rollback aborted before any file mutation |

### Integration Tests

- **Happy path:** full cycle with gitignore guard, running/completed records, `.agent-context/` cleanup, commit
- **Resume after failure:** task 2 failed → re-runs task 2; earlier tasks unchanged
- **Discrepancy/consistency checks:** stale record → abort; archived with no latest → abort
- **Verification retry:** FAIL → retry with latest reasoning only; exhausted → `failed`; non-zero → fail without verification
- **Agent timeout:** subprocess killed → retry with original prompt; always-timing-out → `failed`
- **Rollback:** `src/` reverted, new commit, record chain restored; blocked by dirty tree; blocked by skipped record; commit failure after mutations → clear error
- **Agent binary not found:** `OSError` caught → `failed` record, lock released
- **SIGINT during verification:** subprocess killed, lock released, `running` record persists → NEEDS_CONFIRMATION
- **SIGINT during agent:** running record remains; confirm → re-runs; `base_commit` matches interrupted record
- **Explicit targeting:** intermediates with no record get skipped; existing records of intermediate tasks (including `failed`) left untouched; target task runs even when it already has a successful record (old record archived); discrepancy/consistency errors abort before confirmation
- **Startup tmp cleanup:** `.tmp` in `results/` → deleted → dirty-tree check passes
- **Empty git repo / Detached HEAD:** abort with clear error before writing any records
- **Zombie reaping:** SIGINT → `process.wait()` called → process not in zombie state
- **Workspace overwrite:** confirm → delete + recopy; deny → abort; **live metrics:** kill mid-run → partial metrics in running record
- **Lock contention:** second `agent-build run` while first holds lock → clear error
- **Missing config:** no `agent-build.config.json` → all defaults, run proceeds; **no-change commit:** no file changes → commit aborts with clear error
