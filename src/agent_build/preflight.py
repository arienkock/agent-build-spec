from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path
from typing import Optional

import click


class PreflightError(Exception):
    pass


def cleanup_tmp_files(results_dir: Path) -> None:
    """Delete leftover .tmp files in results/ before the dirty-tree check."""
    if results_dir.exists():
        for p in results_dir.iterdir():
            if p.suffix == ".tmp":
                try:
                    p.unlink()
                except OSError:
                    pass


def check_git_repo(project_root: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_root,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PreflightError("Project root is not a git repository.")


def check_required_dirs(project_root: Path) -> None:
    for name in ("src", "tasks", "verifications"):
        d = project_root / name
        if not d.exists() or not d.is_dir():
            raise PreflightError(f"Required directory '{name}/' does not exist.")


def check_clean_tree(project_root: Path) -> None:
    """Check working tree; prompt user if dirty. Raise PreflightError if declined."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        click.echo("Warning: the working tree has uncommitted or untracked changes:")
        click.echo(result.stdout.rstrip())
        if not click.confirm("Proceed anyway?"):
            raise PreflightError("Aborted: dirty working tree.")


def get_head_commit(project_root: Path) -> str:
    """Return current HEAD commit hash. Raises PreflightError if no commits exist."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PreflightError(
            "git rev-parse HEAD failed — the repository has no commits. "
            "Rollback requires a base commit; create an initial commit before running."
        )
    return result.stdout.strip()


def check_not_detached_head(project_root: Path) -> None:
    """Raise PreflightError if HEAD is detached."""
    result = subprocess.run(
        ["git", "symbolic-ref", "HEAD"],
        cwd=project_root,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PreflightError(
            "Repository is in detached HEAD state. "
            "Commits made in detached HEAD state may become unreachable. "
            "Check out a branch before running."
        )


def _pid_exists(pid: int) -> bool:
    """Return True if a process with this PID is currently running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists; we just cannot signal it


def _is_agent_build_process(pid: int) -> Optional[bool]:
    """
    Return True if the PID is an agent-build process, False if not, None if uncertain.
    Uses `ps` for cross-platform compatibility.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False  # Process no longer exists
        return "agent-build" in result.stdout
    except OSError:
        return None  # Cannot execute ps


def acquire_lock(lock_path: Path) -> None:
    """
    Acquire the project lock file using O_CREAT | O_EXCL.
    Applies stale-lock resolution per spec:
      - Absent PID (process gone) → remove stale lock and acquire
      - PID alive + cmdline matches agent-build → refuse (live process)
      - PID alive + cmdline mismatch or unreadable → refuse (cannot confirm stale)
      - Non-parseable or empty lock file → refuse, naming the path
    Raises PreflightError if the lock cannot be acquired.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return  # Lock acquired successfully
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise PreflightError(f"Failed to create lock file: {exc}") from exc

    # Lock file already exists — analyze it
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        raise PreflightError(
            f"Lock file exists at {lock_path} but could not be read. "
            "Remove it manually if no agent-build run is in progress."
        )

    if not content:
        raise PreflightError(
            f"Lock file exists at {lock_path} but is empty. "
            "Remove it manually if no agent-build run is in progress."
        )

    try:
        pid = int(content)
    except ValueError:
        raise PreflightError(
            f"Lock file at {lock_path} contains non-parseable content. "
            "Remove it manually if no agent-build run is in progress."
        )

    if not _pid_exists(pid):
        # Stale lock: PID is gone → remove and acquire
        click.echo(
            f"Stale lock file found (PID {pid} is no longer running). "
            "Removing and proceeding."
        )
        lock_path.unlink(missing_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
        except OSError as exc:
            raise PreflightError(
                f"Failed to acquire lock after removing stale lock: {exc}"
            ) from exc
        return

    # PID is alive — check if it's really agent-build
    is_agent = _is_agent_build_process(pid)
    if is_agent is True:
        raise PreflightError(
            f"Another agent-build run is in progress (PID {pid}). "
            f"Lock file: {lock_path}"
        )
    # is_agent is False (PID reused by another process) or None (cmdline unreadable)
    raise PreflightError(
        f"Lock file exists at {lock_path} (PID {pid}) but the running process "
        "could not be confirmed as agent-build. "
        "Remove it manually if no agent-build run is in progress."
    )


def release_lock(lock_path: Path) -> None:
    """Release the lock file. Safe to call even if the lock does not exist."""
    try:
        lock_path.unlink()
    except OSError:
        pass


def run(project_root: Path) -> str:
    """
    Run all preflight checks in order. Returns the base commit hash.
    The caller MUST call release_lock(project_root / '.agent-build.lock')
    in a finally block after this returns successfully.
    """
    results_dir = project_root / "results"

    # 1. Delete .tmp files before the dirty-tree check
    cleanup_tmp_files(results_dir)

    # 2. Verify git repo, required dirs, clean working tree
    check_git_repo(project_root)
    check_required_dirs(project_root)
    check_clean_tree(project_root)

    # 3. Ensure there is at least one commit (rollback requires a base)
    base_commit = get_head_commit(project_root)

    # 4. Ensure we are not in detached HEAD state
    check_not_detached_head(project_root)

    # 5. Acquire the project lock
    lock_path = project_root / ".agent-build.lock"
    acquire_lock(lock_path)

    return base_commit
