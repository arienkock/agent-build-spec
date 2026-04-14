# Agent Build Spec — Implementation Plan

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
  cli.py           — entry point: commands, output rendering (init, status, run)
```

`types.py`, `project.py`, `results.py`, `resume.py` are written in Phase 1 and remain stable. Each phase extends `task_run.py` and `cli.py`.

## Phases

- [Phase 0: Project Initialization](phases/phase-0.md)
- [Phase 1: Core Structure + Resume Point](phases/phase-1.md)
- [Phase 2: Preflight + Workspace + Commits](phases/phase-2.md)
- [Phase 3: Agent Invocation](phases/phase-3.md)
- [Phase 4: Verifications + Retry Loop](phases/phase-4.md)
- [Phase 5: Rollback](phases/phase-5.md)
- [Phase 6: Progress & Observability](phases/phase-6.md)

## Testing

`pytest` with `tmp_path` fixtures. No subprocess mocking except where noted. Integration tests use a real temp git repo and a fake agent subprocess (configurable exit code). See individual phase documents for detailed test requirements.
