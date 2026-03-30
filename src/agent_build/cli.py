from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .config import ConfigError, load_config
from .preflight import PreflightError
from .project import ProjectError, load_tasks
from .results import ResultsStore, ResultsStoreError
from .resume import determine_resume_point
from .task_run import TaskRunError, run_task
from .types import ResumePointKind, Task, TaskRunStatus
from .workspace import WorkspaceError

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def _find_project_root() -> Path:
    """Use the current working directory as the project root."""
    return Path.cwd()


@click.group()
def cli() -> None:
    """agent-build: structured task execution for coding agents."""


@cli.command()
def status() -> None:
    """Show the status of all tasks."""
    root = _find_project_root()

    try:
        tasks = load_tasks(root)
    except ProjectError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    store = ResultsStore(root / "results")

    rows: list[tuple[str, str]] = []
    for task in tasks:
        try:
            record = store.get_latest(task.id)
        except Exception:
            record = None
        status_str = record.status.value if record else "—"
        rows.append((task.id, status_str))

    if not rows:
        click.echo("No tasks found.")
        return

    id_width = max(len(r[0]) for r in rows)
    header = f"{'TASK':<{id_width}}  STATUS"
    click.echo(header)
    click.echo("-" * len(header))
    for task_id, status_val in rows:
        click.echo(f"{task_id:<{id_width}}  {status_val}")


def _execute_task(
    root: Path,
    task: Task,
    store: ResultsStore,
    config: object,
    tasks_to_skip: list[Task] | None = None,
) -> None:
    """Run a single task, translating known errors to user-facing messages."""
    try:
        run_task(root, task, store, config, tasks_to_skip=tasks_to_skip)  # type: ignore[arg-type]
    except (PreflightError, WorkspaceError, TaskRunError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _run_explicit_task(
    root: Path,
    target_id: str,
    tasks: list[Task],
    store: ResultsStore,
    config: object,
) -> None:
    """
    Handle `agent-build run <task-id>` (explicit task targeting).

    Order of operations:
    1. Abort if task ID is not found.
    2. Discrepancy check — abort before confirmation if it fails.
    3. Consistency check — abort before confirmation if it fails.
    4. If target task's latest record is RUNNING → NEEDS_CONFIRMATION prompt.
       Otherwise → normal "Run task X?" confirmation.
    5. Write SKIPPED records for tasks before target that have no latest record.
    6. Execute the target task.
    """
    # 1. Find the target task
    target: Task | None = next((t for t in tasks if t.id == target_id), None)
    if target is None:
        click.echo(f"Error: task '{target_id}' not found.", err=True)
        sys.exit(1)

    target_index = tasks.index(target)

    # 2. Discrepancy check
    task_ids = {t.id for t in tasks}
    results_ids = store.task_ids_in_results()
    unknown_ids = results_ids - task_ids
    if unknown_ids:
        listed = ", ".join(sorted(unknown_ids))
        click.echo(
            f"Error: Results directory contains records for unknown task ID(s): {listed}. "
            "Resolve the discrepancy manually before proceeding.",
            err=True,
        )
        sys.exit(1)

    # 3. Consistency check
    inconsistent = store.check_consistency()
    if inconsistent:
        listed = ", ".join(sorted(inconsistent))
        click.echo(
            f"Error: Task(s) have archived records but no latest record: {listed}. "
            "The results directory is in an inconsistent state.",
            err=True,
        )
        sys.exit(1)

    # 4. Prompt for confirmation
    try:
        target_record = store.get_latest(target.id)
    except ResultsStoreError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if target_record is not None and target_record.status == TaskRunStatus.RUNNING:
        click.echo(
            f"Task '{target.id}' has status 'running'. "
            "This may indicate a concurrent or interrupted run."
        )
        if not click.confirm("Re-run this task from the beginning?"):
            click.echo("Aborted.")
            sys.exit(0)
    else:
        if not click.confirm(f"Run task '{target.id}'?"):
            click.echo("Aborted.")
            sys.exit(0)

    # 5. Collect tasks before target that have no latest record → will be
    #    written as SKIPPED inside run_task, after preflight clears the
    #    dirty-tree check (avoids a spurious second prompt).
    tasks_to_skip: list[Task] = []
    for task in tasks[:target_index]:
        try:
            record = store.get_latest(task.id)
        except ResultsStoreError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        if record is None:
            tasks_to_skip.append(task)

    # 6. Execute the target task (skipped records written inside after lock)
    _execute_task(root, target, store, config, tasks_to_skip=tasks_to_skip)


@cli.command()
@click.argument("task_id", required=False)
def run(task_id: str | None) -> None:
    """Run the next task (or a specific task by ID)."""
    root = _find_project_root()

    try:
        config = load_config(root)
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    try:
        tasks = load_tasks(root)
    except ProjectError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    store = ResultsStore(root / "results")

    if task_id is not None:
        _run_explicit_task(root, task_id, tasks, store, config)
        return

    # Normal resume flow
    resume = determine_resume_point(tasks, store)

    if resume.kind == ResumePointKind.ERROR:
        click.echo(f"Error: {resume.message}", err=True)
        sys.exit(1)

    if resume.kind == ResumePointKind.COMPLETE:
        click.echo("All tasks are complete. Nothing to run.")
        return

    if resume.kind == ResumePointKind.NEEDS_CONFIRMATION:
        assert resume.task is not None
        click.echo(
            f"Task '{resume.task.id}' has status 'running'. "
            "This may indicate a concurrent or interrupted run."
        )
        if not click.confirm("Re-run this task from the beginning?"):
            click.echo("Aborted.")
            sys.exit(0)

    assert resume.task is not None
    _execute_task(root, resume.task, store, config)
