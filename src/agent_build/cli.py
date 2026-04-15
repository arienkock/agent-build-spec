from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .config import ConfigError, load_config
from .init import InitError, init_project
from .preflight import PreflightError
from .project import ProjectError, load_tasks
from .results import ResultsStore, ResultsStoreError
from .resume import determine_resume_point
from .rollback import RollbackError, perform_rollback
from .task_run import TaskRunError, run_task
from .types import ResumePointKind, Task, TaskRunStatus
from .workspace import WorkspaceError

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

_STATUS_STYLE: dict[str, dict] = {
    "completed": {"fg": "bright_green"},
    "failed": {"fg": "red"},
    "running": {"fg": "yellow"},
    "skipped": {"fg": "white", "dim": True},
    "—": {"fg": "white", "dim": True},
}


def _find_project_root() -> Path:
    """Use the current working directory as the project root."""
    return Path.cwd()


@click.group()
def cli() -> None:
    """
    agent-build: structured task execution for coding agents.

    Learn more about the agent-build-spec standard at:
    https://github.com/arienkock/agent-build-spec/blob/master/agent-build-spec.md
    """


@cli.command()
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Remove existing files and reinitialize.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompts (use with --force).",
)
@click.option(
    "--git",
    is_flag=True,
    default=None,
    help="Initialize a git repository.",
)
@click.option(
    "--global",
    "global_flag",
    is_flag=True,
    default=False,
    help="Create global/GLOBAL.md with a template header.",
)
@click.option(
    "--template",
    type=str,
    default="minimal",
    help="Template to use (default: minimal).",
)
@click.argument("project_root", type=click.Path(), default=".")
def init(
    force: bool,
    yes: bool,
    git: bool | None,
    global_flag: bool,
    template: str,
    project_root: str,
) -> None:
    """Initialize a new agent-build project."""
    import os
    from .init import init_project, list_builtin_templates

    root = Path(project_root).resolve()

    if git is None:
        git = not (root / ".git").exists()

    try:
        init_project(
            root,
            force=force,
            git=git,
            global_flag=global_flag,
            template=template if template != "minimal" else None,
            yes=yes,
        )
    except InitError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Initialized agent-build project at {root}")


@cli.command()
@click.argument("task_id")
def history(task_id: str) -> None:
    """Show execution history for a specific task."""
    root = _find_project_root()

    from .project import load_tasks

    tasks = load_tasks(root)
    if not tasks:
        click.echo("No tasks found in project.", err=True)
        sys.exit(1)

    task = next((t for t in tasks if t.id == task_id), None)
    if not task:
        click.echo(f"Task '{task_id}' not found.", err=True)
        sys.exit(1)

    store = ResultsStore(root / "results")

    # Check if there are any results at all
    if not store.results_dir.exists():
        click.echo(f"No execution history found for task '{task_id}'.")
        return

    # Gather all results for this task (latest + archived)
    import re
    import json

    _LATEST_RE = re.compile(r"^results-(.+)\.json$")
    _ARCHIVED_RE = re.compile(r"^results-(.+)--run-(\d+)\.json$")

    history_records = []

    for p in store.results_dir.iterdir():
        m_archived = _ARCHIVED_RE.match(p.name)
        if m_archived and m_archived.group(1) == task_id:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                record = store._from_dict(data)
                history_records.append(
                    {
                        "order": int(m_archived.group(2)),
                        "is_latest": False,
                        "record": record,
                        "filename": p.name,
                    }
                )
            except Exception:
                pass
            continue

        m_latest = _LATEST_RE.match(p.name)
        if m_latest and m_latest.group(1) == task_id:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                record = store._from_dict(data)
                history_records.append(
                    {
                        "order": float("inf"),
                        "is_latest": True,
                        "record": record,
                        "filename": p.name,
                    }
                )
            except Exception:
                pass

    if not history_records:
        click.echo(f"No execution history found for task '{task_id}'.")
        return

    # Sort chronologically (by order, with latest at the end)
    history_records.sort(key=lambda x: x["order"])

    click.echo(f"Execution History for Task: {task_id}")
    click.echo("=" * 60)

    for item in history_records:
        record = item["record"]
        is_latest = item["is_latest"]

        status_color = (
            "green"
            if record.status == "completed"
            else "red"
            if record.status == "failed"
            else "yellow"
        )

        label = "LATEST" if is_latest else f"RUN {item['order']}"
        click.echo(f"[{label}] Status: ", nl=False)
        click.secho(record.status.value.upper(), fg=status_color, bold=True)

        if record.start_time:
            click.echo(f"  Started: {record.start_time}")
        if record.end_time:
            click.echo(f"  Ended:   {record.end_time}")

        metrics = []
        if record.input_tokens is not None:
            metrics.append(f"In Tokens: {record.input_tokens}")
        if record.output_tokens is not None:
            metrics.append(f"Out Tokens: {record.output_tokens}")
        if record.cost is not None:
            metrics.append(f"Cost: ${record.cost:.4f}")

        if metrics:
            click.echo(f"  Metrics: {', '.join(metrics)}")

        if record.base_commit:
            click.echo(f"  Base Commit: {record.base_commit[:7]}")

        click.echo("-" * 60)


@cli.command()
def status() -> None:
    """Show the status of all tasks and what will run next."""
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
        styled = click.style(status_val, **_STATUS_STYLE.get(status_val, {}))
        click.echo(f"{task_id:<{id_width}}  {styled}")

    # Footer: show what would run next
    resume = determine_resume_point(tasks, store)
    click.echo("")
    if resume.kind == ResumePointKind.READY:
        assert resume.task is not None
        click.echo(f"Next: run task '{resume.task.id}'")
    elif resume.kind == ResumePointKind.NEEDS_CONFIRMATION:
        assert resume.task is not None
        click.echo(
            f"Next: re-run task '{resume.task.id}' "
            "(status: running — may be interrupted)"
        )
    elif resume.kind == ResumePointKind.COMPLETE:
        click.echo("All tasks complete.")
    elif resume.kind == ResumePointKind.ERROR:
        click.echo(f"Error: {resume.message}", err=True)


def _execute_task(
    root: Path,
    task: Task,
    store: ResultsStore,
    config: object,
    tasks_to_skip: list[Task] | None = None,
    yes: bool = False,
    skip_build: bool = False,
    agent_output_mode: str = "append",
) -> None:
    """Run a single task, translating known errors to user-facing messages."""
    try:
        run_task(  # type: ignore[arg-type]
            root,
            task,
            store,
            config,
            tasks_to_skip=tasks_to_skip,
            yes=yes,
            skip_build=skip_build,
            agent_output_mode=agent_output_mode,
        )
    except (PreflightError, WorkspaceError, TaskRunError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _run_explicit_task(
    root: Path,
    target_id: str,
    tasks: list[Task],
    store: ResultsStore,
    config: object,
    yes: bool = False,
    skip_build: bool = False,
    agent_output_mode: str = "append",
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
        if not yes and not click.confirm("Re-run this task from the beginning?"):
            click.echo("Aborted.")
            sys.exit(0)
    else:
        if not yes and not click.confirm(f"Run task '{target.id}'?"):
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
    _execute_task(
        root,
        target,
        store,
        config,
        tasks_to_skip=tasks_to_skip,
        yes=yes,
        skip_build=skip_build,
        agent_output_mode=agent_output_mode,
    )


@cli.command()
@click.argument("task_id", required=False)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Auto-confirm all prompts (useful for scripting).",
)
@click.option(
    "--skip-build",
    is_flag=True,
    default=False,
    help=(
        "Skip agent invocation; run verifications and commit if they pass. "
        "Useful after manually fixing src/."
    ),
)
@click.option(
    "--agent-output-mode",
    type=click.Choice(["append", "ui", "hidden"]),
    default="append",
    help="How to display agent output (default: append).",
)
def run(
    task_id: str | None, yes: bool, skip_build: bool, agent_output_mode: str
) -> None:
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
        _run_explicit_task(
            root,
            task_id,
            tasks,
            store,
            config,
            yes=yes,
            skip_build=skip_build,
            agent_output_mode=agent_output_mode,
        )
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
        if not yes and not click.confirm("Re-run this task from the beginning?"):
            click.echo("Aborted.")
            sys.exit(0)

    assert resume.task is not None
    _execute_task(
        root,
        resume.task,
        store,
        config,
        yes=yes,
        skip_build=skip_build,
        agent_output_mode=agent_output_mode,
    )


@cli.command()
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Auto-confirm all prompts (useful for scripting).",
)
def rollback(yes: bool) -> None:
    """Rollback the latest task run."""
    root = _find_project_root()
    try:
        perform_rollback(root, yes=yes)
    except RollbackError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
