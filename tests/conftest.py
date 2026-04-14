from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


@pytest.fixture
def project_root(tmp_path):
    """
    Minimal valid agent-build project:
      - initialised git repo with one commit
      - tasks/001-first/TASK.md
      - verifications/001-check.md
      - src/ directory
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    (repo / "tasks" / "001-first").mkdir(parents=True)
    (repo / "tasks" / "001-first" / "TASK.md").write_text(
        "# First Task\nDo something.\n"
    )
    (repo / "verifications").mkdir()
    (repo / "verifications" / "001-check.md").write_text("# Check\nVerify something.\n")
    (repo / "src").mkdir()

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return repo


@pytest.fixture
def two_task_project(tmp_path):
    """
    Project with two tasks: 001-first and 002-second.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    for task_id in ("001-first", "002-second"):
        (repo / "tasks" / task_id).mkdir(parents=True)
        (repo / "tasks" / task_id / "TASK.md").write_text(
            f"# {task_id}\nDo something.\n"
        )
    (repo / "verifications").mkdir()
    (repo / "verifications" / "001-check.md").write_text("# Check\nVerify.\n")
    (repo / "src").mkdir()

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return repo


def run_cli(cwd: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Invoke the real agent-build CLI as a subprocess."""
    return subprocess.run(
        ["agent-build", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
    )


def make_agent_config(
    project_root: Path,
    exit_code: int = 0,
    create_file: str | None = None,
    verification_fail: bool = False,
    fail_reason: str = "Test failure",
    verification_exit_code: int = 0,
    max_retries: int = 0,
    verification_timeout_seconds: int = 10,
    fail_if_stdin_contains: str | None = None,
    invocation_count_file: str | None = None,
    verification_no_output: bool = False,
    verification_raw_output: str | None = None,
    verification_create_file: str | None = None,
) -> None:
    """
    Write agent-build.config.json pointing at fake_agent.py.

    The fake agent detects whether it's being called as a verification agent
    (stdin contains the sentinel "Respond with a JSON object on the last line")
    and responds accordingly.

    Agent mode: reads stdin, optionally creates a file, exits with exit_code.
    Verification mode: outputs PASS/FAIL JSON based on verification_* flags.
    """
    import shlex

    cmd = f"{sys.executable} {FAKE_AGENT} --exit-code {exit_code} --model {{model}}"
    if create_file:
        cmd += f" --create-file {shlex.quote(create_file)}"
    if verification_fail:
        cmd += " --verification-fail"
    if fail_reason != "Test failure":
        cmd += f" --fail-reason {shlex.quote(fail_reason)}"
    if verification_exit_code != 0:
        cmd += f" --verification-exit-code {verification_exit_code}"
    if fail_if_stdin_contains is not None:
        cmd += f" --fail-if-stdin-contains {shlex.quote(fail_if_stdin_contains)}"
    if invocation_count_file is not None:
        cmd += f" --invocation-count-file {shlex.quote(invocation_count_file)}"
    if verification_no_output:
        cmd += " --verification-no-output"
    if verification_raw_output is not None:
        cmd += f" --verification-raw-output {shlex.quote(verification_raw_output)}"
    if verification_create_file is not None:
        cmd += f" --verification-create-file {shlex.quote(verification_create_file)}"

    cmd += " {prompt}"

    cfg = {
        "agent_command": cmd,
        "agent_timeout_seconds": 10,
        "verification_timeout_seconds": verification_timeout_seconds,
        "max_retries": max_retries,
    }
    (project_root / "agent-build.config.json").write_text(json.dumps(cfg))


def write_result(project_root: Path, task_id: str, status: str) -> None:
    """
    Inject a minimal result record directly into results/ to set up test state.
    """
    results_dir = project_root / "results"
    results_dir.mkdir(exist_ok=True)
    record = {"status": status, "previousResults": None}
    (results_dir / f"results-{task_id}.json").write_text(json.dumps(record))
