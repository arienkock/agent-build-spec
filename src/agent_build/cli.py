from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .config import ConfigError, load_config
from .project import ProjectError, load_tasks
from .results import ResultsStore
from .resume import determine_resume_point
from .types import ResumePointKind

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


@cli.command()
@click.argument("task_id", required=False)
def run(task_id: str | None) -> None:
    """Determine the resume point and (stub) run the next task."""
    root = _find_project_root()

    try:
        load_config(root)
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)

    try:
        tasks = load_tasks(root)
    except ProjectError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    store = ResultsStore(root / "results")
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
    click.echo(f"Resume point: {resume.task.id}")
    click.echo("(Execution stubbed — Phase 1)")
