# Implementation Plan: Agent Build Spec

## Stack

Python 3.11+, full type hints, `click` CLI, command: `agent-build`, package: `agent_build`.

## Module Layout

```
agent_build/
  types.py         — Task, ResultRecord, ResumePoint dataclasses/enums
  config.py        — Config dataclass, loaded from agent-build.config.json
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

`types.py`, `project.py`, `results.py`, `resume.py` written in Phase 1 and remain stable. Each phase extends `task_run.py` and `cli.py`.

---

## Phase 1 — Core Structure + Resume Point

**CLI:** `agent-build status` (task table), `agent-build run` (prints resume point, execution stubbed).

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
- `get_latest(task_id)` raises `ResultsStoreError` on malformed JSON; returns `None` for non-existent task
- `check_consistency()` returns task IDs with archived records but no latest record
- `write()` creates results dir if absent; archives existing latest atomically (temp → rename) before writing new
- Archive numbering: `max_existing_archived_order + 1` (not count — correctly handles gaps from manual deletion)

### resume.py Algorithm

0. `check_consistency()` non-empty → ERROR
1. Record references unknown task ID → ERROR; `ResultsStoreError` from `get_latest()` → ERROR
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

Prompt always passed via stdin, never interpolated into `agent_command`. Command split to argv list, `shell=False`.

---

## Phase 2 — Preflight + Workspace + Commits

**Preflight:** verify git repo, required dirs (`src/`, `tasks/`, `verifications/`), clean working tree (prompt if dirty), acquire lock at `<project_root>/.agent-build.lock` via `O_CREAT | O_EXCL | O_WRONLY`. Stale lock: check PID exists AND cmdline matches `agent-build`; refuse if unverifiable. **Non-parseable or corrupted lock file content is also "unverifiable" — refuse with a clear error naming the lock file path.**

**Startup temp file cleanup (CRITICAL):** Before preflight dirty-tree check, scan and delete any `results/*.tmp` files left by a previously crashed atomic write. These files are git-untracked (in a tracked directory) and would otherwise permanently block the dirty-tree check with no user-actionable path forward. Perform this cleanup unconditionally at startup, before any other preflight step.

**Workspace:** copy task dir → `src/.agent-context/task/`, `global/` → `src/.agent-context/global/` (skip if absent), `verifications/` → `src/.agent-context/verifications/`. Confirm before overwriting existing `.agent-context/`; if confirmed, delete then recopy.

**Gitignore guard (CRITICAL):** Before copying, ensure `.agent-context` is in `src/.gitignore` (append/create if needed). Without this, failed runs leave untracked files that block subsequent preflight checks and rollback.

**Records:** write `running` record (with `base_commit`) before agent; write `completed` after. Remove `src/.agent-context/` before committing (success path only). Commit stages only `src/` and `results/`; abort cleanly if no changes in either.

**Task ordering:** lexicographic on full directory name. Accept formats like `001b-setup-extra`, `01.1-init`. Emit WARNING (not error) for names with no leading alphanumeric prefix. Abort if any task dir lacks `TASK.md`; use distinct errors for empty `tasks/` vs. `tasks/` absent.

**Explicit task targeting** (`agent-build run <task-id>`): run consistency check and discrepancy check first (abort before confirmation prompt if either fails). The discrepancy check for explicit targeting additionally aborts if any intermediate task (ordered before `<task-id>`) has a `failed` latest record — the user must resolve or re-run those tasks first. After confirmation, write `skipped` records for intermediate tasks with no latest record (do not overwrite existing records); run target task normally.

---

## Phase 3 — Agent Invocation

### Initial Task Prompt Template

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

The conditional global block is included if and only if `src/.agent-context/global/GLOBAL.md` was copied (i.e., `global/GLOBAL.md` exists in the project root). This prompt is the "original prompt" that timeout retries reproduce verbatim and that verification failure retries prepend.

- Subprocess: no TTY, captured stdio, `cwd=<root>/src/`, prompt via stdin
- Events: `AgentStarted`, `AgentOutput(chunk)`, `AgentCompleted(exit_code)`, `AgentTimedOut`
- Timeout → retry with identical original prompt (shared `max_retries` counter)
- Non-zero exit → no retry → `failed` record
- **`OSError` on subprocess launch (binary not found, not executable, permission denied):** catch `OSError` around the `subprocess.Popen` call; treat as a hard failure — write `failed` record with synthetic error message, do not retry. Never let the exception propagate past `agent.py` without writing a record and releasing the lock.
- SIGINT/SIGTERM: kill subprocess, re-raise; `running` record remains. Next run shows NEEDS_CONFIRMATION → after user confirms, **read `base_commit` from the existing running record first**, then overwrite with a new `running` record using that preserved `base_commit`, and re-run from scratch. The read-then-overwrite must be treated as an atomic sequence within the task run orchestration — do not overwrite until `base_commit` is safely in memory.
- Lock released in `finally` block (including `KeyboardInterrupt`)
- Live metric updates: subscribe to agent events and update `running` record with partial token/timing metrics as they arrive

---

## Phase 4 — Verifications + Retry Loop

- Run only after agent exits code 0; non-zero exit → skip verification → fail path
- No verification files → skip entirely, treat as all-pass
- Run verifications in lexicographic order; **halt on first FAIL**; subprocess `cwd=<root>/src/`
- **Verification prompt** (per spec appendix): verification file content verbatim, then append:
  - `The task instructions are in .agent-context/task/TASK.md. Read them before making your assessment.`
  - `Respond with a single JSON object on the last line of your output. Do not include any text after the JSON object. { "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }`
- Parse **last non-empty line** as `{"status": "PASS"|"FAIL", "reasoning": "..."}`. Empty/whitespace, non-zero exit, timeout, or non-JSON → FAIL with synthetic reasoning (never propagate parse exception). Timeout uses `verification_timeout_seconds`.
- **`OSError` on verification subprocess launch:** same rule as agent — catch `OSError` around `subprocess.Popen`; treat as FAIL with synthetic reasoning. Do not propagate the exception.
- **SIGINT/SIGTERM during verification:** kill the verification subprocess, re-raise the signal. The outer `finally` block releases the lock; the `running` record is left in place (same as agent SIGINT). The next run detects NEEDS_CONFIRMATION and re-runs the full task (workspace is already prepared — no re-copy needed, but verifications restart from the beginning).
- **Retry prompt** (per spec appendix): `{original_prompt}\n\n---\n\nThe following verification failed. Review the reasoning and correct the issue.\n\nVerification: \`.agent-context/verifications/{id}.md\`\n\nReasoning:\n{reasoning}` — only most recent failure appended, not accumulated. `{id}` = filename stem of the verification file (filename without the `.md` extension, e.g. `01-check-output` for `01-check-output.md`).
- Timeout and verification-failure retries share `max_retries`; exhausted → `failed` record, non-zero exit, `src/` left as-is for inspection

---

## Phase 5 — Rollback

Command: `agent-build rollback`

Guards checked in order before touching any files:
1. No uncommitted/untracked changes
2. Latest record not `skipped`
3. Base commit in git history
4. If `previousResults` non-null: verify referenced archive exists and is valid JSON

Actions: restore `src/` to base commit; delete latest record; if `previousResults` non-null, rename (move) archive to be the new latest record (not copy — archive file is consumed); create new commit (no history rewrite) staging `src/` and `results/`. `previousResults: null` → delete latest record only; `src/` revert still committed.

**Rollback partial failure (HIGH):** All git operations and record mutations must be sequenced so that no record file is modified until all git filesystem operations succeed. Order: (1) `git checkout <base_commit> -- src/`, (2) delete latest record, (3) rename archive → latest if applicable, (4) `git add src/ results/`, (5) `git commit`. If step 5 fails (e.g., pre-commit hook), `src/` and records are already mutated but no new commit exists. The system **MUST** surface this as a clear error with the current state described ("`src/` has been reset, results records updated, but the rollback commit could not be created"). Do not attempt to undo the filesystem mutations — the user can inspect and manually commit or revert. This is an acceptable known failure mode.

---

## Phase 6 — Progress & Observability

Extends `agent.py`, `cli.py`. Stream token/cost metrics; periodic diff of `src/` vs base commit; spinners and progress bar; `agent-build history <task-id>`.

---

## Testing

`pytest` with `tmp_path` fixtures. No subprocess mocking except where noted. Integration tests use real temp git repo and fake agent subprocess (configurable exit code).

### Unit Tests

| Module | Key cases |
|---|---|
| `project.py` | Lexicographic sort; missing `TASK.md` → error; empty `tasks/` → distinct error from absent |
| `resume.py` | Consistency → ERROR before discrepancy; all skipped → COMPLETE; gap → ERROR; running at last → NEEDS_CONFIRMATION; running not at last → ERROR; failed → READY; completed with remaining → READY (next) |
| `preflight.py` | `O_EXCL` atomicity; stale PID with mismatched cmdline → refuse; absent PID → acquire; `src/` absent → error; corrupted/non-parseable lock file → refuse with error message naming lock file path; tmp `.tmp` files deleted before dirty-tree check (not after) |
| `workspace.py` | `.agent-context` added to gitignore; no duplicate append; global absent → skip; existing `.agent-context/` triggers confirm |
| `agent.py` | Prompt via stdin not argv; `cwd=src/`; SIGINT kills subprocess; timeout → kill + timeout event; on resume after SIGINT, `base_commit` read from existing running record before overwrite; `OSError` on launch → `failed` record + clear message, no retry |
| `verification.py` | Last non-empty line parsed; empty/non-JSON/timeout → FAIL; `cwd=src/`; timeout uses `verification_timeout_seconds`; no files → skip; lexicographic order; halt on first FAIL; retry `{id}` is filename stem not full filename; `OSError` on launch → FAIL with synthetic reasoning; SIGINT → kill subprocess + re-raise |
| `task_run.py` | `max_retries` exhausted → failed + non-zero exit; lock released on exception; global prompt conditional on GLOBAL.md actually copied; retry prompt format (latest failure only); agent non-zero → verification skipped; `.agent-context/` removed on success only; commit aborts cleanly if no changes; timeout retry uses original unchanged prompt; explicit targeting with failed intermediate → abort before confirmation; `base_commit` from prior running record preserved into new running record (read before overwrite, not after) |
| `results.py` | Malformed JSON → `ResultsStoreError`; non-existent → `None`; archive numbering with gaps; atomic write; `check_consistency()` detects broken chain; `write()` creates results dir if absent |
| `rollback.py` | Missing `previousResults` file → abort before changes; null chain → delete only; skipped → abort; missing base commit → abort; guard order enforced; archive file is moved not copied — original archive absent after successful rollback |

### Integration Tests

- **Happy path:** full cycle with gitignore guard, running/completed records, `.agent-context/` cleanup, commit
- **Resume after failure:** task 2 failed → re-runs task 2; earlier tasks unchanged
- **Discrepancy check:** stale record for deleted task → abort before side effects
- **Consistency check:** archived with no latest → abort
- **Verification retry:** FAIL → retry with latest reasoning only; all PASS → completed; non-zero exit → fail without verification
- **Agent timeout:** subprocess killed → retry with original prompt; `max_retries=1` always-timing-out → `failed`
- **Verification exhaustion:** `max_retries=1` always-failing verification → `failed`; lock released; `src/` left as-is
- **Rollback:** `src/` reverted, new commit, record chain restored; blocked by dirty tree; blocked by skipped record; git commit failure after record mutations → clear error describing partial state, no silent crash
- **Agent binary not found:** `agent_command` points to non-existent binary → `OSError` caught → `failed` record written → lock released cleanly
- **SIGINT during verification:** verification subprocess killed, lock released, `running` record persists; next run → NEEDS_CONFIRMATION
- **Explicit targeting:** intermediate tasks get skipped records; discrepancy check aborts before confirmation; intermediate task with `failed` record aborts before confirmation
- **SIGINT:** subprocess killed, running record remains, lock released; next run → NEEDS_CONFIRMATION; confirm → re-runs from scratch; `base_commit` in new running record matches value from the interrupted running record (not a fresh HEAD)
- **Startup tmp cleanup:** `.tmp` file in `results/` left by crashed write → deleted at startup → dirty-tree check passes → run proceeds normally
- **Lexicographic ordering:** `001-a`, `001b-extra`, `002-b` sorted correctly
- **Workspace overwrite:** confirm → delete + recopy; deny → abort
- **Live metrics:** kill mid-run → partial metrics captured in running record
