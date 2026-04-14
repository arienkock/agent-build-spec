# Phase 1 — Core Structure + Resume Point

**CLI:** `agent-build status` (task table), `agent-build run` (prints resume point; execution stubbed).

## Key Types

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

## results.py

- Filenames: latest `results-<id>.json`, archived `results-<id>--run-<N>.json`
- JSON keys are camelCase (`previousResults`, `baseCommit`, `startTime`, etc.)
- Skipped records contain only `status` and `previousResults`
- `get_latest(task_id)` raises `ResultsStoreError` on malformed JSON; returns `None` if absent
- `check_consistency()` returns task IDs with an archived record but no latest record
- `write()` creates results dir if absent; archives existing latest atomically (temp → rename) before writing new
- Archive numbering: `max_existing_archived_order + 1` (handles gaps)

## resume.py Algorithm

1. **Discrepancy check (first):** any task ID found in `results/` filenames with no matching task directory → ERROR
2. `check_consistency()` non-empty, or `ResultsStoreError` from `get_latest()` → ERROR
3. Multiple `running` tasks → ERROR
4. No records → READY (first task)
5. Find `last_task`: highest-ordered task with a latest record
6. All tasks before `last_task` must be `completed` or `skipped`; any other status or absent record → ERROR
7. `last_task` status: `running` → NEEDS_CONFIRMATION; `failed` → READY (re-run); `completed`/`skipped` → READY (next task) or COMPLETE (none remain)

## config.py Defaults

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

## Testing

| Module | Key cases |
|---|---|
| `project.py` | Lexicographic sort; missing `TASK.md` → error; empty vs. absent `tasks/` distinct; no leading alphanumeric → WARNING only; `001b-setup-extra`, `01.1-init` accepted |
| `resume.py` | Discrepancy check first (unknown ID in latest AND archived → ERROR); consistency (archived without latest → ERROR); no records → READY; all completed/skipped → COMPLETE; gap → ERROR; running at last → NEEDS_CONFIRMATION; running not at last → ERROR; failed → READY; single-task project with that task `completed` → COMPLETE; all tasks `skipped` → COMPLETE (skipped treated as success) |
| `config.py` | Missing file → all defaults; zero/negative timeout → error; extra fields ignored; `{0}` in command → error; `{unknown}` in command → error; `{model}` only → valid; `{model}` substituted but remaining `{something}` present → validation error; empty `agent_command` string → validation error; argv split for `shell=False` |
| `results.py` | Malformed JSON → `ResultsStoreError`; valid JSON missing `status` field → `ResultsStoreError`; non-existent → `None`; archive numbering with gaps; atomic write; consistency detects broken chain; `write()` creates dir; skipped serializes only `status` + `previousResults`; camelCase keys |
