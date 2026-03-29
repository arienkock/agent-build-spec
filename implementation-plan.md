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
- JSON keys are camelCase (`previousResults`, `baseCommit`, `startTime`, etc.)
- Skipped records contain only `status` and `previousResults`
- `get_latest(task_id)` raises `ResultsStoreError` on malformed JSON; returns `None` if absent
- `check_consistency()` returns task IDs with an archived record but no latest record
- `write()` creates results dir if absent; archives existing latest atomically (temp → rename) before writing new
- Archive numbering: `max_existing_archived_order + 1` (handles gaps)

### resume.py Algorithm

1. **Discrepancy check (first):** any task ID found in `results/` filenames with no matching task directory → ERROR
2. `check_consistency()` non-empty, or `ResultsStoreError` from `get_latest()` → ERROR
3. Multiple `running` tasks → ERROR
4. No records → READY (first task)
5. Find `last_task`: highest-ordered task with a latest record
6. All tasks before `last_task` must be `completed` or `skipped`; any other status or absent record → ERROR
7. `last_task` status: `running` → NEEDS_CONFIRMATION; `failed` → READY (re-run); `completed`/`skipped` → READY (next task) or COMPLETE (none remain)

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
- Missing fields use defaults; unknown fields ignored; zero/negative timeout → validation error.
- **`{model}` substitution:** `{0}` (IndexError) or `{unknown}` (KeyError) must be caught at config validation, not runtime. After stripping `{model}`, any remaining `{...}` → validation error.

---

## Phase 2 — Preflight + Workspace + Commits

**Preflight order:**
1. **CRITICAL:** Delete `results/*.tmp` files before dirty-tree check (crash artifacts block it permanently).
2. Verify git repo, required dirs (`src/`, `tasks/`, `verifications/`), clean working tree (prompt if dirty).
3. **CRITICAL:** `git rev-parse HEAD` must succeed — no commits means rollback is impossible. Abort with clear error.
4. **HIGH:** `git symbolic-ref HEAD` must succeed — detached HEAD makes commits unreachable. Abort with clear error.
5. Acquire lock at `<project_root>/.agent-build.lock` via `O_CREAT | O_EXCL | O_WRONLY`. Stale lock resolution: absent PID → acquire; PID present AND cmdline matches `agent-build` → refuse (live process); PID present but cmdline doesn't match (PID reused by another process) OR cmdline unreadable (permission denied, etc.) → refuse (cannot confirm lock is truly stale). Non-parseable or empty lock file → refuse, naming the lock file path.

**Workspace:** copy task dir → `src/.agent-context/task/`, `global/` → `src/.agent-context/global/` (skip if absent), `verifications/` → `src/.agent-context/verifications/`. Confirm before overwriting existing `.agent-context/`; if confirmed, delete then recopy.

**CRITICAL — Gitignore guard:** Before copying, ensure `.agent-context` is in `src/.gitignore` (append/create if needed). Without this, failed runs leave untracked files that block subsequent preflight checks and rollback.

**Records:** write `running` record (with `base_commit`) before agent; write `completed` after. Remove `src/.agent-context/` before committing (success path only). Commit stages only `src/` and `results/`; abort cleanly if no changes.

**CRITICAL — Retries:** Preflight and workspace prep run exactly once per task run. All retries (timeout or verification failure) skip both steps and reuse the prepared workspace.

**Task ordering:** lexicographic on full directory name. Accept formats like `001b-setup-extra`, `01.1-init`. Emit WARNING (not error) for names without a leading alphanumeric prefix. Abort if any task dir lacks `TASK.md`; use distinct errors for empty vs. absent `tasks/`.

**Explicit task targeting** (`agent-build run <task-id>`): run discrepancy and consistency checks first (abort before confirmation if either fails). Write `skipped` records for intermediate tasks with no latest record; do not overwrite existing records of intermediate tasks (including `failed`). The target task always runs — archiving its existing record normally. **Exception:** if the target task's latest record is `running`, prompt for confirmation before proceeding (same NEEDS_CONFIRMATION behavior as the normal resume flow), then archive the running record and re-run.

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
- Timeout → retry with original prompt (shared `max_retries` counter); non-zero exit → fail, no retry
- `OSError` on launch → `failed` record, no retry, no propagation
- **HIGH — SIGINT/SIGTERM:** Install a SIGTERM handler that raises `SystemExit`. Without it, Python's default SIGTERM skips `finally` blocks, leaks the lock, and leaves the subprocess running. Handler must: kill subprocess → `process.wait()` (reap zombie) → `raise SystemExit(1)`. The `SystemExit` propagates through `finally` blocks, releasing the lock. `running` record persists. Next run: NEEDS_CONFIRMATION → reads `base_commit` from existing record before overwriting.

---

## Phase 4 — Verifications + Retry Loop

- Run only after agent exits 0; non-zero exit → skip verification → fail
- No verification files → skip entirely (treat as all-pass)
- Run in lexicographic order; halt on first FAIL; `cwd=<root>/src/`
- **Verification prompt:** verbatim file content, then append: task instructions reference and JSON-response instruction: `{ "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }`
- Parse **last non-empty line** as JSON. Empty output, non-JSON, non-zero exit, or timeout → FAIL with synthetic reasoning (never propagate parse exceptions).
- `OSError` on launch → FAIL with synthetic reasoning; do not propagate
- **SIGINT during verification:** kill subprocess, re-raise; lock released in outer `finally`; `running` record persists → NEEDS_CONFIRMATION on next run
- **Retry prompt:** `{original_prompt}\n\n---\n\nThe following verification failed. Review the reasoning and correct the issue.\n\nVerification: \`.agent-context/verifications/{id}.md\`\n\nReasoning:\n{reasoning}` — `{id}` is the filename stem; only the most recent failure is appended.
- Timeout and verification-failure retries share `max_retries`; exhausted → `failed` record, `src/` left as-is for inspection

---

## Phase 5 — Rollback

Command: `agent-build rollback`

**HIGH — Lock acquisition:** Acquire the project lock (same `O_EXCL` mechanism) before any guards or file mutations. Without this, concurrent `rollback` and `run` can corrupt both `src/` and `results/` simultaneously. Same stale-lock rules apply; release in `finally`.

**Guards (checked in order before touching files):**
1. No uncommitted/untracked changes
2. Latest record not `skipped`
3. Base commit in git history
4. If `previousResults` non-null: referenced archive exists and is valid JSON

**Actions:** restore `src/` to base commit; delete latest record; if `previousResults` non-null, **move** (not copy) archive to become new latest; commit staging `src/` and `results/`.

**HIGH — Partial failure:** If `git commit` fails (e.g., pre-commit hook), `src/` and records are already mutated. Surface clear error: "`src/` has been reset, results records updated, but the rollback commit could not be created." Do not undo filesystem mutations.

---

## Phase 6 — Progress & Observability

Extends `agent.py`, `cli.py`. Stream token/cost metrics; periodic diff of `src/` vs base commit; spinners and progress bar; `agent-build history <task-id>`.

---

## Testing

`pytest` with `tmp_path` fixtures. No subprocess mocking except where noted. Integration tests use a real temp git repo and a fake agent subprocess (configurable exit code).

### Unit Tests

| Module | Key cases |
|---|---|
| `project.py` | Lexicographic sort; missing `TASK.md` → error; empty vs. absent `tasks/` distinct; no leading alphanumeric → WARNING only; `001b-setup-extra`, `01.1-init` accepted |
| `resume.py` | Discrepancy check first (unknown ID in latest AND archived → ERROR); consistency (archived without latest → ERROR); no records → READY; all completed/skipped → COMPLETE; gap → ERROR; running at last → NEEDS_CONFIRMATION; running not at last → ERROR; failed → READY |
| `preflight.py` | `O_EXCL` atomicity; stale PID with mismatched cmdline → refuse; absent PID → acquire; corrupted/empty lock → refuse naming path; `.tmp` deleted before dirty-tree check; empty repo → abort; detached HEAD → abort |
| `workspace.py` | `.agent-context` added to gitignore; no duplicate append; global absent → skip; existing `.agent-context/` triggers confirm |
| `agent.py` | Prompt via stdin not argv; `cwd=src/`; SIGINT kills then `wait()` reaps; SIGTERM handler installed → kills + `wait()` + raises `SystemExit`; timeout → kill + `wait()` + event; resume preserves `base_commit`; `OSError` → failed, no retry |
| `verification.py` | Last non-empty line parsed; non-zero/empty/non-JSON/timeout → FAIL synthetic; `cwd=src/`; no files → skip; lexicographic halt on first FAIL; `{id}` is filename stem; `OSError` → FAIL synthetic; SIGINT → kill + re-raise |
| `task_run.py` | `max_retries` exhausted → failed; lock released on exception; global prompt conditional; retry prompt has latest failure only; non-zero → verification skipped; `.agent-context/` removed on success only; commit aborts if no changes; explicit targeting: failed intermediate untouched; completed target archives old record; shared counter exhausted by mixed timeout+verification failures; workspace prep and preflight not repeated on any retry |
| `config.py` | Missing file → all defaults; zero/negative timeout → error; extra fields ignored; `{0}` in command → error; `{unknown}` in command → error; `{model}` only → valid; argv split for `shell=False` |
| `results.py` | Malformed JSON → `ResultsStoreError`; non-existent → `None`; archive numbering with gaps; atomic write; consistency detects broken chain; `write()` creates dir; skipped serializes only `status` + `previousResults`; camelCase keys |
| `rollback.py` | Missing `previousResults` file → abort before changes; null chain → delete only; skipped → abort; missing base commit → abort; guard order enforced; archive moved not copied; no-change → clear error, no empty commit; lock already held → abort before mutation |

### Integration Tests

- **Happy path:** full cycle with gitignore guard, running/completed records, `.agent-context/` cleanup, commit
- **Resume after failure:** task 2 failed → re-runs task 2; earlier tasks unchanged
- **Discrepancy/consistency checks:** stale record → abort; archived with no latest → abort
- **Verification retry:** FAIL → retry with latest reasoning only; exhausted → failed; non-zero → fail without verification
- **Agent timeout:** subprocess killed → retry with original prompt; always-timing-out → failed
- **Rollback:** `src/` reverted, new commit, record chain restored; blocked by dirty tree; blocked by skipped; commit failure after mutations → clear error
- **Agent binary not found:** `OSError` → failed record, lock released
- **SIGINT/SIGTERM during agent:** running record remains; `process.wait()` reaps zombie; confirm → re-runs; `base_commit` matches interrupted record
- **SIGINT during verification:** subprocess killed, lock released, running record persists → NEEDS_CONFIRMATION
- **Explicit targeting:** intermediates with no record get skipped; existing failed intermediate left untouched; target runs even with successful record (old archived); discrepancy/consistency errors abort before confirmation; target with `running` record → NEEDS_CONFIRMATION prompt → confirmed → archives running record and runs
- **Startup tmp cleanup:** `.tmp` in `results/` → deleted → dirty-tree check passes
- **Empty git repo / Detached HEAD:** abort with clear error before writing any records
- **Lock contention:** second `agent-build run` while first holds lock → clear error
- **Missing config:** no `agent-build.config.json` → all defaults, run proceeds
- **No-change commit:** no file changes → abort with clear error
