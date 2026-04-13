from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import make_agent_config, run_cli, write_result


def test_run_yes_skips_confirmation(project_root):
    """--yes flag bypasses the 'Run task?' prompt without any stdin input."""
    make_agent_config(project_root)

    result = run_cli(project_root, "run", "--yes")

    # Should not hang on prompt and should succeed
    assert result.returncode == 0


def test_run_happy_path(project_root):
    """Fake agent exits 0 → completed result record written, new git commit created."""
    make_agent_config(project_root, exit_code=0)

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 0

    # Result record must exist and be 'completed'
    record_path = project_root / "results" / "results-001-first.json"
    assert record_path.exists(), "Result record file not created"
    record = json.loads(record_path.read_text())
    assert record["status"] == "completed"

    # A new git commit must exist beyond the initial one
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=project_root, capture_output=True, text=True, check=True,
    )
    commits = log.stdout.strip().splitlines()
    assert len(commits) >= 2, "Expected at least 2 commits after a successful run"
    assert "001-first" in commits[0]


def test_run_result_record_structure(project_root):
    """Result JSON after happy path has all required fields."""
    make_agent_config(project_root, exit_code=0)
    run_cli(project_root, "run", "--yes")

    record_path = project_root / "results" / "results-001-first.json"
    record = json.loads(record_path.read_text())

    assert record["status"] == "completed"
    assert "baseCommit" in record
    assert "startTime" in record
    assert "endTime" in record
    assert record["baseCommit"]  # non-empty


def test_run_agent_failure(project_root):
    """Fake agent exits 1 → failed result record, no new commit, CLI exits 1."""
    make_agent_config(project_root, exit_code=1)

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1

    # Result record must show 'failed'
    record_path = project_root / "results" / "results-001-first.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text())
    assert record["status"] == "failed"

    # No extra commit should exist
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=project_root, capture_output=True, text=True, check=True,
    )
    commits = log.stdout.strip().splitlines()
    assert len(commits) == 1, "No new commit should be created after agent failure"


def test_run_second_task_after_first_complete(two_task_project):
    """
    With task 1 already completed (injected record), run → picks up task 2.
    """
    write_result(two_task_project, "001-first", "completed")
    make_agent_config(two_task_project, exit_code=0)

    result = run_cli(two_task_project, "run", "--yes")

    assert result.returncode == 0
    record_path = two_task_project / "results" / "results-002-second.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text())
    assert record["status"] == "completed"


def test_run_explicit_with_skip(two_task_project):
    """
    Explicit targeting of task 2 with no prior results:
    task 1 gets a 'skipped' record, task 2 runs.
    """
    make_agent_config(two_task_project, exit_code=0)

    result = run_cli(two_task_project, "run", "002-second", "--yes")

    assert result.returncode == 0

    skipped_path = two_task_project / "results" / "results-001-first.json"
    assert skipped_path.exists()
    assert json.loads(skipped_path.read_text())["status"] == "skipped"

    completed_path = two_task_project / "results" / "results-002-second.json"
    assert completed_path.exists()
    assert json.loads(completed_path.read_text())["status"] == "completed"


def test_run_resume_after_failure(project_root):
    """
    Injected 'failed' record → run retries the same task.
    The new record should be 'completed' after a successful agent run.
    """
    write_result(project_root, "001-first", "failed")
    make_agent_config(project_root, exit_code=0)

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 0
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "completed"


def test_run_running_record_shows_warning(project_root):
    """Injected 'running' record → output warns about interrupted run."""
    write_result(project_root, "001-first", "running")

    # Pass 'n' to abort after seeing the prompt
    result = run_cli(project_root, "run", stdin="n\n")

    assert "may indicate a concurrent or interrupted run" in result.stdout


def test_run_lock_released_on_completion(project_root):
    """After a successful run the lock file must not exist."""
    make_agent_config(project_root, exit_code=0)
    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 0
    assert not (project_root / ".agent-build.lock").exists()


def test_run_lock_released_on_failure(project_root):
    """After a failed agent run the lock file must not exist."""
    make_agent_config(project_root, exit_code=1)
    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    assert not (project_root / ".agent-build.lock").exists()


def test_run_progress_messages(project_root):
    """Progress messages appear in stdout during a successful run."""
    make_agent_config(project_root, exit_code=0)
    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 0
    assert "Running preflight checks" in result.stdout
    assert "Preparing workspace" in result.stdout
    assert "Invoking agent" in result.stdout
    assert "completed in" in result.stdout


def test_run_status_shows_complete_after_run(project_root):
    """After a successful run, `status` shows the task as completed."""
    make_agent_config(project_root, exit_code=0)
    run_cli(project_root, "run", "--yes")

    status_result = run_cli(project_root, "status")
    assert "completed" in status_result.stdout
    assert "All tasks complete." in status_result.stdout


def test_run_short_flag_y(project_root):
    """-y is accepted as an alias for --yes."""
    make_agent_config(project_root, exit_code=0)
    result = run_cli(project_root, "run", "-y")
    assert result.returncode == 0
