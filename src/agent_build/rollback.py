from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click

from . import preflight
from .project import load_tasks
from .results import ResultsStore, ResultsStoreError
from .types import TaskRunStatus


class RollbackError(Exception):
    pass


def perform_rollback(project_root: Path, yes: bool = False) -> None:
    """
    Rollback the latest task run.
    Restores src/ to the base commit, updates results records, and creates a commit.
    """
    lock_path = project_root / ".agent-build.lock"
    lock_acquired = False

    try:
        # HIGH: Acquire the project lock before any guards or file mutations.
        preflight.acquire_lock(lock_path)
        lock_acquired = True

        store = ResultsStore(project_root / "results")

        # Guard 0: At least one latest result record exists
        tasks = load_tasks(project_root)

        # Find the latest task that has a record
        target_task_id = None
        target_record = None
        for task in reversed(tasks):
            try:
                record = store.get_latest(task.id)
                if record is not None:
                    target_task_id = task.id
                    target_record = record
                    break
            except ResultsStoreError as exc:
                raise RollbackError(
                    f"Error reading record for {task.id}: {exc}"
                ) from exc

        if target_task_id is None or target_record is None:
            raise RollbackError("No task records found; nothing to roll back.")

        # Guard 1: No uncommitted/untracked changes
        try:
            preflight.check_clean_tree(project_root, yes=yes)
        except preflight.PreflightError as exc:
            raise RollbackError(str(exc)) from exc

        # Guard 2: Latest record not `skipped`
        if target_record.status == TaskRunStatus.SKIPPED:
            raise RollbackError(
                f"Task '{target_task_id}' was skipped. "
                "Skipped tasks do not modify the workspace and cannot be rolled back."
            )

        # Guard 3: Base commit in git history
        base_commit = target_record.base_commit
        if not base_commit:
            raise RollbackError(
                f"Task '{target_task_id}' has no base commit recorded. "
                "Cannot restore the workspace."
            )

        cat_file = subprocess.run(
            ["git", "cat-file", "-e", f"{base_commit}^{{commit}}"],
            cwd=project_root,
            capture_output=True,
        )
        if cat_file.returncode != 0:
            raise RollbackError(
                f"Base commit '{base_commit}' not found in git history. "
                "Cannot restore the workspace."
            )

        # Guard 4: If previousResults non-null: referenced archive exists and is valid JSON
        prev_archive_path = None
        if target_record.previous_results is not None:
            prev_archive_path = store.results_dir / target_record.previous_results
            if not prev_archive_path.exists():
                raise RollbackError(
                    f"Referenced previous results archive '{target_record.previous_results}' is missing. "
                    "The results store is in an inconsistent state."
                )
            try:
                with prev_archive_path.open("r", encoding="utf-8") as f:
                    json.load(f)
            except json.JSONDecodeError as exc:
                raise RollbackError(
                    f"Referenced previous results archive '{target_record.previous_results}' is invalid JSON: {exc}"
                ) from exc

        # Prompt for confirmation
        if not yes:
            click.echo(
                f"Ready to rollback task '{target_task_id}'.\n"
                f"This will restore 'src/' to commit {base_commit[:7]} and remove the latest result record."
            )
            if not click.confirm("Proceed with rollback?"):
                click.echo("Aborted.")
                return

        # Actions
        click.echo(f"Rolling back task '{target_task_id}'...")

        # Restore src/ to base commit
        restore = subprocess.run(
            [
                "git",
                "restore",
                "--worktree",
                "--staged",
                "--source",
                base_commit,
                "--",
                "src/",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if restore.returncode != 0 and "did not match any file" not in restore.stderr:
            # We haven't mutated anything yet besides the git index/worktree which we can attempt to clean
            raise RollbackError(
                f"Failed to restore src/ from base commit: {restore.stderr.strip()}"
            )

        clean = subprocess.run(
            ["git", "clean", "-fd", "src/"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if clean.returncode != 0:
            raise RollbackError(
                f"Failed to clean untracked files in src/: {clean.stderr.strip()}"
            )

        # git clean might remove the src/ directory entirely if it was untracked. Recreate it.
        (project_root / "src").mkdir(exist_ok=True)

        # Delete latest record
        latest_path = store._latest_path(target_task_id)
        if latest_path.exists():
            latest_path.unlink()

        # If previousResults non-null, move archive to become new latest
        if prev_archive_path is not None and prev_archive_path.exists():
            prev_archive_path.rename(latest_path)

        # Commit staging src/ and results/
        subprocess.run(
            ["git", "add", "src/", "results/"],
            cwd=project_root,
            capture_output=True,
            check=True,
        )

        commit = subprocess.run(
            ["git", "commit", "-m", f"Rollback task: {target_task_id}"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        if commit.returncode != 0:
            detail = commit.stderr.strip() or commit.stdout.strip()
            raise RollbackError(
                "src/ has been reset, results records updated, but the rollback commit could not be created.\n"
                f"Details: {detail}"
            )

        click.echo(f"Rollback complete. Task '{target_task_id}' has been reverted.")

    finally:
        if lock_acquired:
            preflight.release_lock(lock_path)
