# Implementation Plan: Agent Build Spec

## Stack

- **Python 3.11+**, full type hints, `click` CLI, command: `agent-build`, package: `agent_build`

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

`types.py`, `project.py`, `results.py`, `resume.py` are written once in Phase 1 and remain stable. Each phase extends `task_run.py` and `cli.py` without rewriting earlier modules.

---

## Phase 1 — Core Structure + Resume Point

**New:** `types.py`, `config.py`, `project.py`, `results.py`, `resume.py`, `cli.py`

Loads/validates project from disk, reads result records, determines resume point.

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
- `get_latest(task_id)` raises `ResultsStoreError` on malformed JSON (never returns `None` for corrupt files)
- `check_consistency()` returns task IDs with archived records but no latest record
- `write()` creates results dir if absent; archives existing latest atomically (temp → rename) before writing new one
- Archive order: use `max_existing_archived_order + 1` (NOT count of existing archives); this ensures correct numbering even when archived records have been manually removed

### resume.py Algorithm

0. **Consistency check** — `check_consistency()` non-empty → ERROR
1. **Discrepancy check** — record references unknown task ID → ERROR; `ResultsStoreError` from `get_latest()` → ERROR
2. Multiple `running` tasks → ERROR
3. No records → READY (first task)
4. Find `last_task`: highest-ordered task with a latest record
5. All tasks before `last_task` must be `completed` or `skipped`; any other status (including `running`) or absent record → ERROR
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

Prompt is **always passed via stdin**, never interpolated into `agent_command`. Command split into argv list, `shell=False`.

---

## Phase 2 — Preflight + Workspace + Commits

**New:** `preflight.py`, `workspace.py`; extends `results.py`, `task_run.py`, `cli.py`

- Preflight: verify git repo, required dirs (`src/`, `tasks/`, `verifications/`), clean working tree (prompt if dirty), acquire lock file atomically (`O_EXCL`) at `<project_root>/.agent-build.lock`; stale lock: check PID + cmdline, refuse if unverifiable
- Workspace: copy task dir → `src/.agent-context/task/`, `global/` → `src/.agent-context/global/` (skip if absent), `verifications/` → `src/.agent-context/verifications/`; confirm before overwriting existing context (message: "a previous run likely left the directory behind"); delete before recopy if confirmed
- **Gitignore guard:** before copying, ensure `.agent-context` is listed in `src/.gitignore` (append if absent, create file if needed); this is CRITICAL — without it, a failed run leaves untracked files that block subsequent preflight checks and rollback
- Write `running` record (with `base_commit`) before agent; write `completed` after
- Remove `src/.agent-context/` before committing (success path only; leave as-is on failure)
- Commit stages only `src/` and `results/`; abort if no changes in either (both empty)

---

## Phase 3 — Agent Invocation

**New:** `events.py`, `agent.py`; extends `task_run.py`, `cli.py`

- Invoke `agent_command` as subprocess: no TTY, captured stdio, `cwd=<root>/src/`, prompt via stdin
- Events: `AgentStarted`, `AgentOutput(chunk)`, `AgentCompleted(exit_code)`, `AgentTimedOut`
- On timeout: retry with identical prompt (shared `max_retries` counter)
- On non-zero exit: no retry, record as failed
- SIGINT/SIGTERM: kill subprocess, re-raise; `running` record remains (next run shows NEEDS_CONFIRMATION → after user confirms, overwrite with a new `running` record using same base_commit and re-run task from scratch)
- Lock released in `finally` block wrapping entire run

---

## Phase 4 — Verifications + Retry Loop

**New:** `verification.py`; extends `task_run.py`

- Verifications run only after agent exits with code 0; non-zero exit skips verification → fail path
- **No verification files** (empty or absent `verifications/` dir): skip verification entirely, treat as all-pass, proceed to complete
- Run verifications in lexicographic order of filename; each invoked with `cwd=<root>/src/`; **halt on first FAIL** (do not run subsequent verifications)
- **Verification prompt structure** (per spec appendix): reproduce verification file content verbatim, then append a reference to the task instructions ("`The task instructions are in .agent-context/task/TASK.md. Read them before making your assessment.`"), then append the structured response instruction ("`Respond with a single JSON object on the last line of your output. Do not include any text after the JSON object. { "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }`")
- Parse **last non-empty line** of output as `{"status": "PASS"|"FAIL", "reasoning": "..."}`
- Empty/whitespace output, non-zero exit, **timeout (kill subprocess)**, or non-JSON → FAIL with synthetic reasoning (no propagated exception); verification timeout counts as one retry
- **Retry prompt format** (per spec appendix): original prompt verbatim, then `---` separator, then `"The following verification failed. Review the reasoning and correct the issue."`, then `Verification: .agent-context/verifications/<id>.md`, then `Reasoning: <reasoning>`; only the most recent failure is appended (not accumulated)
- Timeout + verification-failure retries share `max_retries`; exhausted → `failed` record, non-zero exit, `src/` left as-is

---

## Phase 5 — Rollback

**New:** `rollback.py`; extends `cli.py`

- `agent-build rollback` command
- Guards (validate before touching files): no uncommitted/untracked changes; latest record not `skipped`; base commit in git history; if `previousResults` is non-null, verify that the referenced archived file exists and is valid JSON (skip this check when `previousResults` is null — there is simply no prior record to restore)
- Restore `src/` to base commit only; delete latest record; promote archived record (via `previousResults` chain) as new latest; create new commit (no history rewrite)
- `previousResults: null` → delete latest record, no archive to promote

---

## Phase 6 — Progress & Observability

Extends `agent.py`, `cli.py`. Stream token/cost metrics; periodic diff of `src/` vs base commit; spinners and progress bar; `agent-build history <task-id>`.

---

## Explicit Task Targeting (add in Phase 2–3)

`agent-build run <task-id>` — run specific task regardless of resume point.

- Discrepancy check and consistency check are **mandatory prerequisites** — abort before confirmation prompt if either fails
- Confirm with user; write `skipped` records for intermediate tasks with no latest record; leave existing records untouched; then run target task normally

---

## Critical Edge Cases

**Task ordering is lexicographic on full directory name.** Formats like `001b-setup-extra` and `01.1-init` are valid — `project.py` must not reject them. Emit a WARNING (not error) for names with no leading alphanumeric prefix. Never sort by discovery order.

**Shell injection prevention.** Prompt is passed via stdin; never interpolated into `agent_command`. Use `subprocess` with arg list and `shell=False`.

**Stale lock: PID reuse.** Check PID exists AND cmdline matches `agent-build`. If unverifiable, refuse and require manual lock removal.

**Malformed result JSON.** `get_latest()` raises `ResultsStoreError` (not `None`). `resume.py` catches it and returns ERROR immediately.

**Atomic archive failure.** `check_consistency()` detects archived record with no latest record → ERROR in resume.

**Verification output parsing.** Parse last non-empty line. Empty/non-JSON/non-zero-exit → FAIL with synthetic reasoning. Never propagate parse exception.

**max_retries exhausted.** Write `failed` record, release lock, exit non-zero. `src/` including `.agent-context/` left as-is for inspection.

**Rollback chain validation.** Verify `previousResults` file exists and is valid before touching any files.

**`results/` auto-created.** `write()` calls `mkdir(parents=True, exist_ok=True)`.

**`cwd=src/` for all subprocesses.** Both `agent.py` and `verification.py` must set `cwd=<root>/src/`. Failure produces silent wrong output.

**`project.py` validation.** Abort if any task directory lacks `TASK.md`; abort if `tasks/` has no subdirectories (distinct error from `tasks/` absent).

**Lock file location.** Must be `<project_root>/.agent-build.lock`. Use `O_CREAT | O_EXCL | O_WRONLY`. Lock released in `finally` block including on `KeyboardInterrupt`.

**Conditional global prompt.** Include global-instructions paragraph in prompt only when `GLOBAL.md` was actually copied.

**`.agent-context/` must be gitignored.** `workspace.py` must append `.agent-context` to `src/.gitignore` before copying context files (create file if absent). Without this: a failed run leaves untracked files that trigger preflight dirty-tree warnings on every subsequent run and cause rollback to abort with "untracked files" error.

**Empty verifications directory.** If `verifications/` has no `.md` files, skip verification phase entirely and proceed directly to complete. Do not abort.

**Verification subprocess timeout.** Kill subprocess → treat as non-zero exit → FAIL with synthetic reasoning → consumes one retry from `max_retries`. Uses `verification_timeout_seconds` (not `agent_timeout_seconds`).

**NEEDS_CONFIRMATION resolution.** After user confirms, discard the stale `running` record by overwriting it with a new `running` record (preserving `base_commit` from the original) and run the task from scratch.

**Retry prompt construction.** On verification failure, the agent retry prompt is: `{original_prompt}\n\n---\n\nThe following verification failed. Review the reasoning and correct the issue.\n\nVerification: \`.agent-context/verifications/{id}.md\`\n\nReasoning:\n{reasoning}`. Only the most recent failure is appended; earlier failures are not accumulated. On timeout retry, the prompt is the original unchanged.

**Live record updates.** `results.py` must subscribe to agent events to update the `running` record with accumulated metrics (input/output tokens, timing) as they arrive — not only at run end. This ensures metrics are partially captured even if the process is killed.

---

## Testing

`pytest` with `tmp_path` fixtures. No subprocess mocking except where noted. Integration tests use a real temp git repo and a fake agent subprocess (configurable exit code).

### Critical Unit Tests

- `project.py`: lexicographic sort of `001-a`, `001b-extra`, `002-b`; alphanumeric IDs accepted; missing `TASK.md` → `ProjectError`; empty `tasks/` → `ProjectError`
- `results.py`: malformed JSON → `ResultsStoreError`; `check_consistency()` detects broken chain; `write()` creates dir; archive order logic (max+1 not count — gaps in numbering handled correctly); atomic temp-rename; `get_latest()` returns `None` for non-existent task (not raising)
- `resume.py`: consistency check → ERROR before discrepancy check; `ResultsStoreError` → ERROR; discrepancy → ERROR; all `skipped` → COMPLETE; gap in records → ERROR; single `running` record for last task → NEEDS_CONFIRMATION; all tasks `completed` → COMPLETE; last task `completed` with remaining unrecorded tasks → READY (next task); last task `failed` → READY (re-run that task); `running` record NOT at last position (earlier task running) → ERROR
- `preflight.py`: `O_EXCL` atomicity at `.agent-build.lock`; stale lock PID exists but cmdline doesn't match `agent-build` → refuse; stale lock PID does not exist → can acquire; `src/` absent → error
- `workspace.py`: `.agent-context` added to `src/.gitignore` if absent; gitignore file created if needed; `.agent-context` already in gitignore → no duplicate appended; `global/` absent → skip global copy without error; existing `.agent-context/` triggers confirmation prompt before overwrite
- `agent.py`: prompt via stdin not argv; `cwd=src/`; SIGINT kills subprocess; timeout → kills subprocess and returns timeout event
- `verification.py`: last non-empty line parsed (trailing blank lines ignored); empty/non-zero/non-JSON/timeout → FAIL with synthetic reasoning; `cwd=src/`; timeout uses `verification_timeout_seconds` not `agent_timeout_seconds`; no verification files → skip (all-pass); verifications run in lexicographic order and halt on first FAIL
- `task_run.py`: `max_retries` exhausted → `failed` record + non-zero exit; shared retry counter across timeout and verification failures; lock released on unhandled exception; `running` record includes `base_commit`; global prompt conditional on `GLOBAL.md` actually copied; NEEDS_CONFIRMATION → new `running` record overwrites old, task re-runs from scratch; retry prompt format matches spec appendix (only most recent verification reasoning appended, not accumulated); agent non-zero exit → verification phase skipped → `failed` record; `.agent-context/` removed before commit on success path only; commit aborts cleanly if no changes in `src/` or `results/`; agent timeout retry uses original unchanged prompt (no reasoning appended)
- `rollback.py`: broken `previousResults` chain (file missing) → abort before any file changes; `previousResults: null` → delete latest record, no archive to promote; latest record is `skipped` → abort; base commit not in git history → abort; guard checks complete before any file is touched (order: uncommitted changes, skipped check, base commit, previousResults chain)

### Critical Integration Tests

- Full happy path: resume → preflight → workspace (gitignore guard) → running record → agent → verification all-pass → completed record → `.agent-context/` cleanup → commit
- Resume after failure: task 2 `failed` → re-runs task 2; earlier tasks unchanged
- Discrepancy check: stale record for deleted task → abort before any side effects
- Consistency check: archived record with no latest → abort
- Verification flow: FAIL → retry with reasoning appended (only latest); all PASS → completed; non-zero exit → verification skipped → `failed`
- Agent timeout: subprocess killed → retry with original prompt (unchanged, no reasoning); shared retry counter decremented; `max_retries=1` with always-timing-out agent → `failed` record after 1 retry
- `max_retries=1` with always-failing verification → `failed` record after 1 retry; lock released; `src/` left as-is
- Rollback: `src/` reverted to base commit, new commit created, record chain correctly restored; blocked by dirty working tree; blocked by `skipped` record
- Explicit targeting happy path: intermediate unrecorded tasks receive `skipped` records; target task runs normally; discrepancy check runs and aborts before confirmation prompt if stale record found
- SIGINT: subprocess killed, `running` record remains, lock released, subsequent run shows NEEDS_CONFIRMATION; after confirmation new `running` record overwrites old and task re-runs from scratch
- Lexicographic ordering: `001-a`, `001b-extra`, `002-b` all accepted and sorted correctly
- Workspace overwrite: existing `.agent-context/` from prior run triggers confirmation; confirmed → deleted and recopied; denied → abort
- Live metric updates: kill agent mid-run → `running` record in results file contains partial token/timing metrics captured before kill
