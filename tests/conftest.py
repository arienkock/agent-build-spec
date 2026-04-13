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
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"],
                   cwd=repo, check=True, capture_output=True)

    (repo / "tasks" / "001-first").mkdir(parents=True)
    (repo / "tasks" / "001-first" / "TASK.md").write_text("# First Task\nDo something.\n")
    (repo / "verifications").mkdir()
    (repo / "verifications" / "001-check.md").write_text("# Check\nVerify something.\n")
    (repo / "src").mkdir()

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def two_task_project(tmp_path):
    """
    Project with two tasks: 001-first and 002-second.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"],
                   cwd=repo, check=True, capture_output=True)

    for task_id in ("001-first", "002-second"):
        (repo / "tasks" / task_id).mkdir(parents=True)
        (repo / "tasks" / task_id / "TASK.md").write_text(f"# {task_id}\nDo something.\n")
    (repo / "verifications").mkdir()
    (repo / "verifications" / "001-check.md").write_text("# Check\nVerify.\n")
    (repo / "src").mkdir()

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=repo, check=True, capture_output=True)
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
) -> None:
    """
    Write agent-build.config.json pointing at fake_agent.py.
    The fake agent reads stdin, optionally creates a file, and exits with exit_code.
    create_file is relative to src/ (the agent's cwd).
    """
    cmd = f"{sys.executable} {FAKE_AGENT} --exit-code {exit_code} --model {{model}}"
    if create_file:
        cmd += f" --create-file {create_file}"
    cfg = {
        "agent_command": cmd,
        "agent_timeout_seconds": 10,
        "max_retries": 0,
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
