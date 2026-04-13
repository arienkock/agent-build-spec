from __future__ import annotations

import os
import subprocess

from conftest import run_cli, write_result


def test_run_fails_not_git_repo(tmp_path):
    """Running in a directory that isn't a git repo → clear error, exit 1."""
    # Minimal structure without .git
    (tmp_path / "tasks" / "001-first").mkdir(parents=True)
    (tmp_path / "tasks" / "001-first" / "TASK.md").write_text("# Task\n")
    (tmp_path / "verifications").mkdir()
    (tmp_path / "src").mkdir()

    result = run_cli(tmp_path, "run", "--yes")

    assert result.returncode == 1
    assert "not a git repository" in result.stderr.lower()


def test_run_fails_missing_src(project_root):
    """src/ directory missing → error mentioning 'src/'."""
    (project_root / "src").rmdir()

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    assert "src" in result.stderr


def test_run_fails_missing_verifications(project_root):
    """verifications/ directory missing → error mentioning 'verifications/'."""
    import shutil
    shutil.rmtree(project_root / "verifications")

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    assert "verifications" in result.stderr


def test_run_all_tasks_complete(project_root):
    """All tasks have 'completed' records → 'All tasks are complete.' exit 0."""
    write_result(project_root, "001-first", "completed")

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 0
    assert "All tasks are complete" in result.stdout


def test_run_fails_detached_head(project_root):
    """Detached HEAD state → error about detached HEAD."""
    # Detach HEAD
    subprocess.run(
        ["git", "checkout", "--detach"],
        cwd=project_root, check=True, capture_output=True,
    )

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    assert "detached HEAD" in result.stderr


def test_run_fails_no_commits(tmp_path):
    """Fresh git init with no commits → error about no commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "tasks" / "001-first").mkdir(parents=True)
    (repo / "tasks" / "001-first" / "TASK.md").write_text("# Task\n")
    (repo / "verifications").mkdir()
    (repo / "src").mkdir()

    result = run_cli(repo, "run", "--yes")

    assert result.returncode == 1
    assert "no commits" in result.stderr.lower()


def test_run_stale_lock_auto_removed(project_root, make_agent_config_fn):
    """Lock file with a dead PID → stale message printed, run proceeds."""
    # Write a lock file with a PID that definitely doesn't exist
    (project_root / ".agent-build.lock").write_text("999999999")
    make_agent_config_fn(project_root)

    result = run_cli(project_root, "run", "--yes")

    assert "Stale lock file found" in result.stdout
    # Run should proceed and complete (or at least not error on lock)
    assert result.returncode == 0


def test_run_active_lock_fails(project_root):
    """Lock file with the current process's own PID → error about concurrent run."""
    # Use our own PID so _pid_exists returns True, and cmdline won't match 'agent-build'
    # but we need a PID that exists. Use os.getpid() of a known process.
    # Write our own PID — the tool will see the process exists but cmdline isn't agent-build,
    # so it refuses with "could not be confirmed as agent-build".
    (project_root / ".agent-build.lock").write_text(str(os.getpid()))

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    assert "Lock file" in result.stderr or "in progress" in result.stderr


def test_run_explicit_task_not_found(project_root):
    """Explicit task ID that doesn't exist → error 'not found', exit 1."""
    result = run_cli(project_root, "run", "999-bogus")

    assert result.returncode == 1
    assert "not found" in result.stderr


def test_run_aborts_on_n_input(project_root):
    """
    Answering 'n' to the explicit-task confirmation prompt → 'Aborted.', exit 0.
    (The normal resume flow has no confirmation for READY state; explicit targeting does.)
    """
    result = run_cli(project_root, "run", "001-first", stdin="n\n")

    assert result.returncode == 0
    assert "Aborted" in result.stdout


def test_run_fails_tasks_dir_missing(project_root):
    """tasks/ directory missing → error about tasks/."""
    import shutil
    shutil.rmtree(project_root / "tasks")

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    assert "tasks" in result.stderr.lower() or "tasks" in result.stdout.lower()


# Helper fixture injected into conftest via a factory pattern
import pytest

@pytest.fixture
def make_agent_config_fn():
    """Returns the make_agent_config helper so individual tests can use it."""
    from conftest import make_agent_config
    return make_agent_config
