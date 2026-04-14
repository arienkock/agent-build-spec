# Phase 5 — Rollback

Command: `agent-build rollback`

**HIGH — Lock acquisition:** Acquire the project lock (same `O_EXCL` mechanism) before any guards or file mutations. Without this, concurrent `rollback` and `run` can corrupt both `src/` and `results/` simultaneously. Same stale-lock rules apply; release in `finally`.

## Guards (checked in order before touching files)

0. **HIGH:** At least one latest result record exists → abort with clear error ("No task records found; nothing to roll back.") if none present. Without this guard, subsequent guards that dereference the latest record throw unhandled exceptions.
1. No uncommitted/untracked changes
2. Latest record not `skipped`
3. Base commit in git history
4. If `previousResults` non-null: referenced archive exists and is valid JSON

## Actions

restore `src/` to base commit; delete latest record; if `previousResults` non-null, **move** (not copy) archive to become new latest; commit staging `src/` and `results/`.

**HIGH — Partial failure:** If `git commit` fails (e.g., pre-commit hook), `src/` and records are already mutated. Surface clear error: "`src/` has been reset, results records updated, but the rollback commit could not be created." Do not undo filesystem mutations.

## Testing

| Module | Key cases |
|---|---|
| `rollback.py` | No records → abort before any mutation (guard #0); missing `previousResults` file → abort before changes; null chain → delete only; skipped → abort; missing base commit → abort; guard order enforced; archive moved not copied; no-change → clear error, no empty commit; lock already held → abort before mutation |

### Integration Tests

- **Rollback:** `src/` reverted, new commit, record chain restored; blocked by dirty tree; blocked by skipped; commit failure after mutations → clear error; no records → abort with clear error before any mutation
- **Commit failure on task completion:** pre-commit hook rejects commit → record reverted from `completed` to `failed`; `src/` changes preserved; subsequent run resumes at that task
