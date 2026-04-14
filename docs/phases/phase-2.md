# Phase 2 — Preflight + Workspace + Commits

**Preflight order:**
1. **CRITICAL:** Delete `results/*.tmp` files before dirty-tree check (crash artifacts block it permanently).
2. Verify git repo, required dirs (`src/`, `tasks/`, `verifications/`), clean working tree (prompt if dirty).
3. **CRITICAL:** `git rev-parse HEAD` must succeed — no commits means rollback is impossible. Abort with clear error.
4. **HIGH:** `git symbolic-ref HEAD` must succeed — detached HEAD makes commits unreachable. Abort with clear error.
5. Acquire lock at `<project_root>/.agent-build.lock` via `O_CREAT | O_EXCL | O_WRONLY`. Stale lock resolution: absent PID → acquire; PID present AND cmdline matches `agent-build` → refuse (live process); PID present but cmdline doesn't match (PID reused by another process) OR cmdline unreadable (permission denied, etc.) → refuse (cannot confirm lock is truly stale). Non-parseable or empty lock file → refuse, naming the lock file path.

**Workspace:** copy task dir → `src/.agent-context/task/`, `global/` → `src/.agent-context/global/` (skip if absent), `verifications/` → `src/.agent-context/verifications/`. Confirm before overwriting existing `.agent-context/`; if confirmed, delete then recopy.

**CRITICAL — Gitignore guard:** Before copying, ensure `.agent-context` appears as a non-negated line in `src/.gitignore` (append/create if needed). Check for an exact line match on `.agent-context` (strip trailing whitespace; ignore `#`-prefixed and `!`-prefixed lines). If absent or only present as a negation (`!.agent-context`), append `.agent-context`. Without this, failed runs leave untracked files that block subsequent preflight checks and rollback.

**Records:** write `running` record (with `base_commit`) before agent; write `completed` after. Remove `src/.agent-context/` before committing (success path only). Commit stages only `src/` and `results/`; abort cleanly if no changes.

**CRITICAL — Commit failure on task completion:** If `git commit` fails during the success path (e.g., pre-commit hook rejects), `src/` and the staged index are already mutated but not committed. Overwrite the `completed` record with a `failed` record (preserving `base_commit`). Surface clear error: "`src/` changes could not be committed — record reverted to `failed`. Inspect and resolve manually." Do not attempt to undo `src/` mutations. This prevents a phantom `completed` state where the next task's `base_commit` would reference the wrong commit.

**CRITICAL — Retries:** Preflight and workspace prep run exactly once per task run. All retries (timeout or verification failure) skip both steps and reuse the prepared workspace.

**Task ordering:** lexicographic on full directory name. Accept formats like `001b-setup-extra`, `01.1-init`. Emit WARNING (not error) for names without a leading alphanumeric prefix. Abort if any task dir lacks `TASK.md`; use distinct errors for empty vs. absent `tasks/`.

**Explicit task targeting** (`agent-build run <task-id>`): run discrepancy and consistency checks first (abort before confirmation if either fails). Write `skipped` records for intermediate tasks with no latest record; do not overwrite existing records of intermediate tasks (including `failed`). The target task always runs — archiving its existing record normally. **Exception:** if the target task's latest record is `running`, prompt for confirmation before proceeding (same NEEDS_CONFIRMATION behavior as the normal resume flow), then archive the running record and re-run.

## Testing

| Module | Key cases |
|---|---|
| `preflight.py` | `O_EXCL` atomicity; stale PID with mismatched cmdline → refuse; absent PID → acquire; corrupted/empty lock → refuse naming path; `.tmp` deleted before dirty-tree check; empty repo → abort; detached HEAD → abort; dirty-tree prompt declined by user → abort before lock write or any mutation |
| `workspace.py` | `.agent-context` added to gitignore; no duplicate append; `!.agent-context` negation present → still appends non-negated line; `.agent-context/` (with trailing slash) in gitignore → bare `.agent-context` still appended (partial match is not an exact match); global absent → skip; existing `.agent-context/` triggers confirm |

### Integration Tests

- **Happy path:** full cycle with gitignore guard, running/completed records, `.agent-context/` cleanup, commit
- **Resume after failure:** task 2 failed → re-runs task 2; earlier tasks unchanged
- **Discrepancy/consistency checks:** stale record → abort; archived with no latest → abort
- **Startup tmp cleanup:** `.tmp` in `results/` → deleted → dirty-tree check passes
- **Empty git repo / Detached HEAD:** abort with clear error before writing any records
- **Lock contention:** second `agent-build run` while first holds lock → clear error
- **Gitignore idempotent:** second run on project that already has `.agent-context` in `src/.gitignore` → no duplicate line appended
- **All tasks skipped via explicit targeting:** target last task with all intermediates having no records → skipped records written for intermediates; after success, `agent-build status` shows COMPLETE
- **Explicit targeting:** intermediates with no record get skipped; existing failed intermediate left untouched; target runs even with successful record (old archived); discrepancy/consistency errors abort before confirmation; target with `running` record → NEEDS_CONFIRMATION prompt → confirmed → archives running record and runs; nonexistent task ID → abort with clear error, no records written, no mutation; target is first task → no skipped records created for intermediates
- **No-change commit:** no file changes → abort with clear error
- **Missing config:** no `agent-build.config.json` → all defaults, run proceeds
