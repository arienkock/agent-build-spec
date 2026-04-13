from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from . import preflight as preflight_mod
from . import workspace as workspace_mod
from .agent import AgentOutcome, build_prompt, run_agent
from .config import Config
from .events import AgentEvent, AgentOutput, EventEmitter
from .preflight import PreflightError, release_lock
from .results import ResultsStore
from .types import ResultRecord, Task, TaskRunStatus
from .verification import VerificationStatus, run_verifications


class TaskRunError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_and_commit(project_root: Path, task_id: str) -> None:
    """
    Stage src/ and results/ (only), then create a commit.
    Raises TaskRunError if there is nothing to commit or if git commit fails.
    """
    stage = subprocess.run(
        ["git", "add", "src/", "results/"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if stage.returncode != 0:
        raise TaskRunError(f"git add failed: {stage.stderr.strip()}")

    # Detect whether anything is actually staged
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=project_root,
        capture_output=True,
    )
    if diff.returncode == 0:
        raise TaskRunError(
            "No changes to commit in src/ or results/. "
            "Nothing was staged after completing the task."
        )

    commit = subprocess.run(
        ["git", "commit", "-m", f"Complete task: {task_id}"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        detail = commit.stderr.strip() or commit.stdout.strip()
        raise TaskRunError(detail)


def _write_failed_record(
    store: ResultsStore,
    task_id: str,
    base_commit: Optional[str],
    start_time: Optional[str],
) -> None:
    """Archive the current latest record and write a FAILED record."""
    prev = store.next_archive_filename(task_id)
    store.write(
        task_id,
        ResultRecord(
            status=TaskRunStatus.FAILED,
            previous_results=prev,
            base_commit=base_commit,
            start_time=start_time,
            end_time=_now_iso(),
        ),
    )


def _build_verification_retry_prompt(
    original_prompt: str,
    verification_id: str,
    reasoning: str,
) -> str:
    """
    Build the retry prompt after a verification failure.

    The original prompt is used as the base each time so that only the most
    recent verification failure reasoning is appended (never accumulated).
    """
    return (
        f"{original_prompt}\n\n"
        f"---\n\n"
        f"The following verification failed. Review the reasoning and correct the issue.\n\n"
        f"Verification: `.agent-context/verifications/{verification_id}.md`\n\n"
        f"Reasoning:\n{reasoning}"
    )


def _on_agent_event(event: AgentEvent) -> None:
    """Default event listener: print agent output to stdout."""
    if isinstance(event, AgentOutput):
        click.echo(event.chunk, nl=False)


def run_task(
    project_root: Path,
    task: Task,
    store: ResultsStore,
    config: Config,
    tasks_to_skip: list[Task] | None = None,
    yes: bool = False,
    skip_build: bool = False,
) -> None:
    """
    Execute the full task-run cycle for *task*:

      preflight → [write skipped records] → running record → workspace prep
      → agent+verification retry loop → completed record → cleanup → commit

    *tasks_to_skip* is an optional list of tasks (preceding the target in
    explicit-targeting mode) that have no latest record and should be written
    as SKIPPED.

    *skip_build* skips agent invocation entirely; verifications still run.
    Useful when the user has manually fixed src/ and wants to verify and commit.

    Preflight and workspace preparation run exactly once per task run; retries
    reuse the already-prepared workspace.  The shared retry counter covers
    timeout retries, and verification-failure retries.

    The project lock is acquired during preflight and released in a finally block.
    SIGTERM installs a handler inside run_agent that kills the subprocess, reaps
    it, and raises SystemExit(1), which propagates through here and releases the
    lock; the RUNNING record then persists for NEEDS_CONFIRMATION on the next run.
    """
    lock_path = project_root / ".agent-build.lock"
    lock_acquired = False

    try:
        # ── Preflight (once per task run; acquires lock) ──────────────────────
        click.echo("Running preflight checks...")
        base_commit = preflight_mod.run(project_root, yes=yes)
        lock_acquired = True

        # ── Write SKIPPED records for any explicit-targeting intermediates ─────
        for skip_task in (tasks_to_skip or []):
            prev_skip = store.next_archive_filename(skip_task.id)
            store.write(
                skip_task.id,
                ResultRecord(
                    status=TaskRunStatus.SKIPPED,
                    previous_results=prev_skip,
                ),
            )

        # ── Write RUNNING record before touching workspace ─────────────────────
        prev_archive = store.next_archive_filename(task.id)
        running_record = ResultRecord(
            status=TaskRunStatus.RUNNING,
            previous_results=prev_archive,
            base_commit=base_commit,
            start_time=_now_iso(),
        )
        store.write(task.id, running_record)

        # ── Workspace preparation (once per task run) ──────────────────────────
        click.echo("Preparing workspace...")
        workspace_mod.prepare(project_root, task, yes=yes)

        # ── Build prompt (once as base; current_prompt mutated on verification retry) ──
        original_prompt = build_prompt(project_root)
        current_prompt = original_prompt

        emitter = EventEmitter()
        emitter.subscribe(_on_agent_event)

        retries_left = config.max_retries

        # ── Outer loop: agent invocation + verification cycle ──────────────────
        while True:

            # ── Agent invocation phase ─────────────────────────────────────────
            if skip_build:
                click.echo("Skipping build step (--skip-build).")
            else:
                click.echo(f"Invoking agent for task '{task.id}'...")

                # Inner timeout-retry sub-loop
                while True:
                    result = run_agent(project_root, config, current_prompt, emitter)

                    if result.outcome == AgentOutcome.LAUNCH_ERROR:
                        _write_failed_record(
                            store, task.id, base_commit, running_record.start_time
                        )
                        raise TaskRunError(
                            f"Failed to launch agent: {result.error}"
                        )

                    if result.outcome == AgentOutcome.FAILED:
                        _write_failed_record(
                            store, task.id, base_commit, running_record.start_time
                        )
                        raise TaskRunError(
                            f"Agent exited with non-zero exit code: {result.exit_code}"
                        )

                    if result.outcome == AgentOutcome.TIMED_OUT:
                        if retries_left <= 0:
                            _write_failed_record(
                                store, task.id, base_commit, running_record.start_time
                            )
                            raise TaskRunError(
                                "Agent timed out and the retry limit is exhausted."
                            )
                        retries_left -= 1
                        click.echo(
                            f"Agent timed out. "
                            f"Retrying ({retries_left} retries remaining)..."
                        )
                        continue  # retry agent with same current_prompt

                    # AgentOutcome.COMPLETED — proceed to verification
                    break

            # ── Verification phase ─────────────────────────────────────────────
            v_status, failing = run_verifications(project_root, config)

            if v_status == VerificationStatus.PASS:
                break  # all verifications passed; proceed to COMPLETED record

            # Verification failed — retry if attempts remain
            assert failing is not None
            if retries_left <= 0:
                _write_failed_record(
                    store, task.id, base_commit, running_record.start_time
                )
                raise TaskRunError(
                    f"Verification '{failing.verification_id}' failed and the "
                    "retry limit is exhausted."
                )
            retries_left -= 1
            click.echo(
                f"Verification '{failing.verification_id}' failed. "
                f"Retrying ({retries_left} retries remaining)..."
            )
            # Rebuild retry prompt from original_prompt so only the most recent
            # failure reasoning is appended (never accumulated).
            current_prompt = _build_verification_retry_prompt(
                original_prompt,
                failing.verification_id,
                failing.reasoning,
            )
            # continue outer loop

        # ── Write COMPLETED record ─────────────────────────────────────────────
        prev_for_completed = store.next_archive_filename(task.id)
        completed_record = ResultRecord(
            status=TaskRunStatus.COMPLETED,
            previous_results=prev_for_completed,
            base_commit=base_commit,
            start_time=running_record.start_time,
            end_time=_now_iso(),
        )
        store.write(task.id, completed_record)

        # ── Remove .agent-context/ before committing ───────────────────────────
        workspace_mod.cleanup(project_root)

        # ── Commit (src/ and results/ only) ───────────────────────────────────
        try:
            _stage_and_commit(project_root, task.id)
        except TaskRunError as exc:
            msg = str(exc)
            if msg.startswith("No changes to commit"):
                raise TaskRunError(msg) from exc
            # Git commit failed (e.g. pre-commit hook rejected the commit).
            # src/ and the staged index are mutated but not committed.
            # Revert the completed record to failed so the next resume point
            # is unambiguous and the task will be re-attempted.
            prev_for_failed = store.next_archive_filename(task.id)
            failed_record = ResultRecord(
                status=TaskRunStatus.FAILED,
                previous_results=prev_for_failed,
                base_commit=base_commit,
                start_time=running_record.start_time,
                end_time=_now_iso(),
            )
            store.write(task.id, failed_record)
            raise TaskRunError(
                "src/ changes could not be committed — record reverted to 'failed'. "
                f"Inspect and resolve manually.\nDetails: {msg}"
            ) from exc

        elapsed = round(
            (datetime.now(timezone.utc) - datetime.fromisoformat(running_record.start_time))
            .total_seconds()
        )
        click.echo(f"Task '{task.id}' completed in {elapsed}s.")

    finally:
        if lock_acquired:
            release_lock(lock_path)
