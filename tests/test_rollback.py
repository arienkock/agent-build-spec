from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import make_agent_config, run_cli


def _get_commits(repo: Path) -> list[str]:
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return log.stdout.strip().splitlines()


def test_rollback_no_records(project_root):
    """Guard 0: abort when no records exist."""
    result = run_cli(project_root, "rollback", "--yes")
    assert result.returncode == 1
    assert "No task records found; nothing to roll back" in result.stderr


def test_rollback_dirty_tree(project_root):
    """Guard 1: abort on dirty tree without --yes."""
    # Run a task successfully first to get a record
    make_agent_config(project_root, exit_code=0)
    run_cli(project_root, "run", "--yes")

    # Dirty the tree
    (project_root / "src" / "dirty.txt").write_text("dirty")

    result = run_cli(project_root, "rollback", stdin="n\n")
    assert result.returncode == 1
    assert "uncommitted or untracked changes" in result.stdout


def test_rollback_skipped_task(two_task_project):
    """Guard 2: abort when rolling back a skipped task."""
    # Target 002-second explicitly, which writes a SKIPPED record for 001-first
    make_agent_config(two_task_project, exit_code=0)
    run_cli(two_task_project, "run", "002-second", "--yes")

    # Fake that 002-second doesn't exist to make 001-first the latest
    (two_task_project / "results" / "results-002-second.json").unlink()

    result = run_cli(two_task_project, "rollback", "--yes")
    assert result.returncode == 1
    assert "was skipped" in result.stderr


def test_rollback_happy_path(project_root):
    """Rollback a successful run: restores src, creates commit, deletes latest record."""
    make_agent_config(project_root, exit_code=0, create_file="new_file.py")
    run_cli(project_root, "run", "--yes")

    assert (project_root / "src" / "new_file.py").exists()
    commits_after_run = _get_commits(project_root)
    assert len(commits_after_run) == 2

    # Perform rollback
    result = run_cli(project_root, "rollback", "--yes")

    assert result.returncode == 0
    assert "Rollback complete" in result.stdout

    # The file should be gone
    assert not (project_root / "src" / "new_file.py").exists()

    # The result record should be deleted, and since it was the first run,
    # the RUNNING record becomes the latest.
    assert (project_root / "results" / "results-001-first.json").exists()
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "running"

    # A new commit should be added
    commits_after_rollback = _get_commits(project_root)
    assert len(commits_after_rollback) == 3
    assert "Rollback task: 001-first" in commits_after_rollback[0]


def test_rollback_with_previous_results(project_root):
    """Rollback restores previousResults archive if it exists."""
    # Write a failed record (run 1)
    results_dir = project_root / "results"
    results_dir.mkdir(exist_ok=True)

    failed_record = {
        "status": "failed",
        "previousResults": None,
        "baseCommit": _get_commits(project_root)[0].split()[0],
    }
    (results_dir / "results-001-first--run-1.json").write_text(
        json.dumps(failed_record)
    )

    # Write a completed record (run 2)
    completed_record = {
        "status": "completed",
        "previousResults": "results-001-first--run-1.json",
        "baseCommit": _get_commits(project_root)[0].split()[0],
    }
    (results_dir / "results-001-first.json").write_text(json.dumps(completed_record))

    result = run_cli(project_root, "rollback", "--yes")
    assert result.returncode == 0

    # The latest record should be the old failed one
    latest_path = project_root / "results" / "results-001-first.json"
    assert latest_path.exists()
    assert not (project_root / "results" / "results-001-first--run-1.json").exists()

    record = json.loads(latest_path.read_text())
    assert record["status"] == "failed"


def test_rollback_partial_failure(project_root):
    """If git commit fails, leave files mutated and surface clear error."""
    make_agent_config(project_root, exit_code=0)
    run_cli(project_root, "run", "--yes")

    # Install a pre-commit hook that always fails
    hook_dir = project_root / ".git" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "pre-commit"
    hook_path.write_text("#!/bin/sh\nexit 1\n")
    hook_path.chmod(0o755)

    result = run_cli(project_root, "rollback", "--yes")

    # It should fail with a specific error
    assert result.returncode == 1
    assert "rollback commit could not be created" in result.stderr

    # But the file operations should have been done (record deleted, RUNNING restored)
    assert (project_root / "results" / "results-001-first.json").exists()
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "running"

    # Remove hook so teardown/other stuff isn't broken
    hook_path.unlink()
