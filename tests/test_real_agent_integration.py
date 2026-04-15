import json
import subprocess
from pathlib import Path
from agent_build.cli import cli


def test_real_agent_run_free_model(tmp_path: Path):
    """
    Realistic integration test using the actual opencode CLI and a free OpenRouter model.
    This asserts that our JSON parsing, stream handling, and metrics extraction
    work end-to-end with a real, live process.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()

    # 1. Initialize project
    init_res = subprocess.run(
        ["agent-build", "init", "--git", "--yes", "--force", str(project_root)],
        capture_output=True,
        text=True,
    )
    assert init_res.returncode == 0

    # 2. Modify config to use the free model
    config_path = project_root / "agent-build.config.json"
    config = json.loads(config_path.read_text())
    config["model"] = "openrouter/openrouter/free"
    # We add a tiny prompt suffix to make it quick and predictable
    config["agent_command"] = (
        "opencode run \"{prompt}. Just output 'OK' and nothing else.\" --model {model} --dangerously-skip-permissions --format json"
    )
    config_path.write_text(json.dumps(config))

    # 3. Create a simple task
    task_dir = project_root / "tasks" / "001-real-test"
    task_dir.mkdir(parents=True)
    (task_dir / "TASK.md").write_text("# Test Task\nThis is a real integration test.\n")

    # Initial commit is required before running agent-build
    subprocess.run(["git", "add", "."], cwd=project_root)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=project_root)

    # 4. Run the task using agent-build
    # We use hidden mode to prevent dirtying stdout, but append is fine too.
    run_res = subprocess.run(
        ["agent-build", "run", "--yes"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    # Check that it completed successfully
    assert run_res.returncode == 0, f"agent-build failed: {run_res.stderr}"

    # 5. Assert the results metadata contains actual metrics
    record_path = project_root / "results" / "results-001-real-test.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text())

    assert record["status"] == "completed"
    assert "inputTokens" in record
    assert "outputTokens" in record
    assert "cost" in record

    # Verify metrics are greater than 0
    assert record["inputTokens"] > 0
    assert record["outputTokens"] > 0
    assert record["cost"] >= 0.0  # Cost can be exactly 0.0 for free models
