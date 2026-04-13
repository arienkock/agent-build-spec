from __future__ import annotations

import json
from pathlib import Path

from conftest import run_cli, write_result


def test_status_no_tasks(tmp_path):
    """tasks/ dir exists but has no task subdirectories → error, exit 1."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tasks").mkdir()

    result = run_cli(repo, "status")

    assert result.returncode == 1
    assert "no task subdirectories" in result.stderr.lower() or "tasks" in result.stderr.lower()


def test_status_tasks_no_results(project_root):
    """Tasks exist but no result files → each row shows '—'."""
    result = run_cli(project_root, "status")

    assert result.returncode == 0
    assert "001-first" in result.stdout
    assert "—" in result.stdout


def test_status_two_tasks_no_results(two_task_project):
    """Both tasks shown with '—' when no results exist."""
    result = run_cli(two_task_project, "status")

    assert result.returncode == 0
    assert "001-first" in result.stdout
    assert "002-second" in result.stdout
    # Both should show no-result marker
    assert result.stdout.count("—") == 2


def test_status_shows_completed(project_root):
    """Injected 'completed' record shows 'completed' in the table."""
    write_result(project_root, "001-first", "completed")

    result = run_cli(project_root, "status")

    assert result.returncode == 0
    assert "completed" in result.stdout


def test_status_shows_failed(project_root):
    """Injected 'failed' record shows 'failed' in the table."""
    write_result(project_root, "001-first", "failed")

    result = run_cli(project_root, "status")

    assert result.returncode == 0
    assert "failed" in result.stdout


def test_status_shows_next_task(project_root):
    """No results → footer says 'Next: run task 001-first'."""
    result = run_cli(project_root, "status")

    assert result.returncode == 0
    assert "Next: run task '001-first'" in result.stdout


def test_status_shows_all_complete(project_root):
    """All tasks completed → footer says 'All tasks complete.'"""
    write_result(project_root, "001-first", "completed")

    result = run_cli(project_root, "status")

    assert result.returncode == 0
    assert "All tasks complete." in result.stdout


def test_status_shows_next_after_first_done(two_task_project):
    """First task complete → footer points at second task."""
    write_result(two_task_project, "001-first", "completed")

    result = run_cli(two_task_project, "status")

    assert result.returncode == 0
    assert "Next: run task '002-second'" in result.stdout


def test_status_running_shows_needs_confirmation(project_root):
    """A 'running' record → footer warns about interrupted run."""
    write_result(project_root, "001-first", "running")

    result = run_cli(project_root, "status")

    assert result.returncode == 0
    assert "may be interrupted" in result.stdout


def test_status_exit_code_zero(project_root):
    """status always exits 0 on a healthy project."""
    result = run_cli(project_root, "status")
    assert result.returncode == 0
