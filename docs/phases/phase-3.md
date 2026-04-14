# Phase 3 — Agent Invocation

## Prompt Template

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

- Subprocess: no TTY, captured stdio, `cwd=<root>/src/`, prompt via argv (substituting `{prompt}` placeholder)
- Events: `AgentStarted`, `AgentOutput(chunk)`, `AgentCompleted(exit_code)`, `AgentTimedOut`
- Timeout → retry with original prompt (shared `max_retries` counter); non-zero exit → fail, no retry
- `OSError` on launch → `failed` record, no retry, no propagation
- **HIGH — SIGINT/SIGTERM:** Install a SIGTERM handler that raises `SystemExit`. Without it, Python's default SIGTERM skips `finally` blocks, leaks the lock, and leaves the subprocess running. Handler must: kill subprocess → `process.wait()` (reap zombie) → `raise SystemExit(1)`. The `SystemExit` propagates through `finally` blocks, releasing the lock. `running` record persists. Next run: NEEDS_CONFIRMATION → reads `base_commit` from existing record before overwriting.

## Testing

| Module | Key cases |
|---|---|
| `agent.py` | Prompt via argv not stdin; `cwd=src/`; SIGINT kills then `wait()` reaps; SIGTERM handler installed → kills + `wait()` + raises `SystemExit`; timeout → kill + `wait()` + event; resume preserves `base_commit`; `OSError` → failed, no retry |

### Integration Tests

- **Agent timeout:** subprocess killed → retry with original prompt; always-timing-out → failed
- **Agent binary not found:** `OSError` → failed record, lock released
- **SIGINT/SIGTERM during agent:** running record remains; `process.wait()` reaps zombie; confirm → re-runs; `base_commit` matches interrupted record
