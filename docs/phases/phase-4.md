# Phase 4 — Verifications + Retry Loop

- Run only after agent exits 0; non-zero exit → skip verification → fail
- No verification files → skip entirely (treat as all-pass)
- Run in lexicographic order; halt on first FAIL; `cwd=<root>/src/`
- **Verification prompt:** verbatim file content, then append: task instructions reference and JSON-response instruction: `{ "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }`
- Parse **last non-empty line** as JSON. Empty output, non-JSON, non-zero exit, or timeout → FAIL with synthetic reasoning (never propagate parse exceptions).
- `OSError` on launch → FAIL with synthetic reasoning; do not propagate
- **SIGINT during verification:** kill subprocess, re-raise; lock released in outer `finally`; `running` record persists → NEEDS_CONFIRMATION on next run
- **Retry prompt:** `{original_prompt}\n\n---\n\nThe following verification failed. Review the reasoning and correct the issue.\n\nVerification: \`.agent-context/verifications/{id}.md\`\n\nReasoning:\n{reasoning}` — `{id}` is the filename stem; only the most recent failure is appended.
- Timeout and verification-failure retries share `max_retries`; exhausted → `failed` record, `src/` left as-is for inspection

## `--skip-build` Flag

`agent-build run --skip-build`: skips the agent invocation entirely and jumps straight to verifications. Intended for the case where the user has manually fixed `src/` after a failed build and wants the tool to verify and commit without re-running the agent.

- Print `"Skipping build step (--skip-build)."` in place of the agent invocation message.
- Verifications proceed as normal; retry loop also still applies — if a verification fails, the user is prompted again.
- If verifications pass (or there are none), the task completes and commits normally.
- Implementation: boolean `skip_build` threaded from `cli.py` → `run_task()` → wraps the agent retry loop in `if not skip_build:`.
- `--skip-build` implies the user accepts the current state of `src/`; it does not interact with `--yes` (confirmation prompts are separate).
- Test cases: `--skip-build` on a fresh task → skips agent, runs verifications, writes completed record; `--skip-build` with failing verification → retry loop activates normally; `--skip-build` + `--yes` → no prompts at all.
- **Pre-committed fix scenario:** the user may have already committed their manual `src/` fix before running `--skip-build`, leaving the working tree clean with no new `src/` changes to stage. This is safe by design: `_stage_and_commit` stages both `src/` and `results/`, and the COMPLETED record in `results/` is always freshly written at this point, so `git diff --cached` will always detect staged changes from `results/` even when `src/` is unchanged. No additional logic is required. Test case: inject a `failed` record, commit a manual fix to `src/`, run `--skip-build` — tool must succeed, commit must exist, and the commit diff must include only `results/` changes.

## Testing

| Module | Key cases |
|---|---|
| `verification.py` | Last non-empty line parsed; non-zero/empty/non-JSON/timeout → FAIL synthetic; `cwd=src/`; no files → skip; lexicographic halt on first FAIL; `{id}` is filename stem; `OSError` → FAIL synthetic; SIGINT → kill + re-raise; multiple verifications: second fails → retry prompt contains only second's reasoning (not first's); JSON with extra fields alongside valid `status: PASS` → valid PASS |
| `task_run.py` | `max_retries` exhausted → failed; lock released on exception; global prompt conditional; retry prompt has latest failure only; non-zero → verification skipped; `.agent-context/` removed on success only; commit aborts if no changes; commit failure on success path → record reverted to `failed`, clear error; explicit targeting: failed intermediate untouched; completed target archives old record; shared counter exhausted by mixed timeout+verification failures; workspace prep and preflight not repeated on any retry; explicit targeting with nonexistent task ID → abort with clear error before any mutation; explicit targeting where target is the first task → no skipped records created; `--skip-build` skips agent loop, verifications still run; `--skip-build` with failing verification → retry loop activates; `--skip-build` + `--yes` → no prompts, verifications proceed |

### Integration Tests

- **Verification retry:** FAIL → retry with latest reasoning only; exhausted → failed; non-zero → fail without verification
- **SIGINT during verification:** subprocess killed, lock released, running record persists → NEEDS_CONFIRMATION
- **Multiple verifications partial failure:** second verification fails → retry prompt contains only second's reasoning; first verification's output not accumulated
- **`--skip-build` happy path:** no agent invoked, verifications run, completed record written, commit created
- **`--skip-build` with failing verification:** verification FAIL triggers retry loop; agent still not invoked on retry; exhausted retries → failed record
- **`--skip-build` + `--yes`:** no confirmation prompts, proceeds directly to verifications
- **`--skip-build` with pre-committed fix:** inject `failed` record; commit a manual file change to `src/`; run `--skip-build` → tool succeeds, COMPLETED record written, new commit created whose diff contains only `results/` (no `src/` changes); no "nothing to commit" error
