import json
import pytest
from conftest import run_cli

def test_history_shows_records(project_root):
    # Create some mock results
    results_dir = project_root / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    
    (results_dir / "results-001-first--run-1.json").write_text(json.dumps({
        "status": "failed",
        "previousResults": None,
        "baseCommit": "abcdef123",
        "startTime": "2023-01-01T12:00:00Z",
        "endTime": "2023-01-01T12:05:00Z",
        "inputTokens": 100,
        "outputTokens": 50,
        "cost": 0.05
    }))
    
    (results_dir / "results-001-first.json").write_text(json.dumps({
        "status": "completed",
        "previousResults": "results-001-first--run-1.json",
        "baseCommit": "abcdef123",
        "startTime": "2023-01-01T12:10:00Z",
        "endTime": "2023-01-01T12:15:00Z",
        "inputTokens": 200,
        "outputTokens": 100,
        "cost": 0.15
    }))

    result = run_cli(project_root, "history", "001-first")
    
    assert result.returncode == 0
    assert "Execution History for Task: 001-first" in result.stdout
    assert "[RUN 1] Status: FAILED" in result.stdout
    assert "[LATEST] Status: COMPLETED" in result.stdout
    assert "In Tokens: 100" in result.stdout
    assert "In Tokens: 200" in result.stdout
    assert "Cost: $0.1500" in result.stdout

def test_history_no_task(project_root):
    result = run_cli(project_root, "history", "nonexistent-task")
    assert result.returncode == 1
    assert "not found" in result.stderr
