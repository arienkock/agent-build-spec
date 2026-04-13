"""
Integration tests for Phase 4: Verifications + Retry Loop.

These tests invoke the real agent-build CLI as a subprocess and verify
end-to-end behavior including verification retries and --skip-build.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import make_agent_config, run_cli, write_result


# ─────────────────────────────────────────────────────────────────────────────
# Verification retry tests
# ─────────────────────────────────────────────────────────────────────────────

def test_verification_pass_after_agent_success(project_root):
    """Agent succeeds and verification passes → completed record, commit."""
    make_agent_config(project_root, create_file="output.txt")

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 0
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "completed"


def test_verification_retry_pass_on_retry(project_root):
    """
    Verification FAILs on first attempt, PASSes on second (after agent retry).
    Final result: completed record.

    Strategy: use --invocation-count-file so the fake agent returns FAIL on
    the first verification call and PASS on the second.  But since we can't
    vary behaviour per invocation number with the existing flags alone, we use
    --fail-if-stdin-contains with a marker that only the *first* verification
    file would contain if they were different.  Instead, we rely on the fact
    that the first run is a FAIL and the retry (second run) will PASS because
    --verification-fail is NOT set — wait, that would always PASS...

    Correct approach: use a counter file.  Odd agent invocations = agent mode
    (exits 0, creates file).  Even invocations = verification mode (FAIL on
    first, PASS on second).  The invocation counter tracks ALL calls.

    Call sequence with max_retries=1:
      1. run_agent call    → invocation 1, agent mode, exits 0
      2. verification call → invocation 2, IS_VERIFICATION, counter=2 → FAIL
      3. run_agent call    → invocation 3, agent mode, exits 0
      4. verification call → invocation 4, IS_VERIFICATION, counter=4 → PASS

    We configure: fail when invocation_num is even.
    fake_agent doesn't natively support "fail on even invocations", but we can
    simulate it differently: configure the first run to fail, then expect the
    retry to pass using a fresh invocation_count_file approach combined with
    two separate configs written sequentially.

    Simplest reliable approach: write a helper script that fails once then passes.
    """
    import sys
    import os

    # Write a stateful fake that FAILs on the first verification call, PASSes
    # thereafter, using an invocation-count-file.
    count_file = str(project_root / ".verif-count")

    # We'll use --fail-if-stdin-contains with a marker we put in the verification
    # file on the first call, then rewrite the file to remove the marker.
    # Easier: write a tiny wrapper script.
    wrapper = project_root / "run_verif.py"
    from pathlib import Path as _P
    import sys as _sys
    wrapper.write_text(f"""\
#!/usr/bin/env python3
import json, sys
from pathlib import Path

count_file = Path({count_file!r})
n = int(count_file.read_text()) if count_file.exists() else 0
n += 1
count_file.write_text(str(n))

stdin = sys.stdin.read()
is_verif = "Respond with a JSON object on the last line" in stdin

if is_verif:
    # Fail on first verification attempt (n==2: after first agent call), pass after
    if n == 2:
        print(json.dumps({{"status": "FAIL", "reasoning": "first attempt fail"}}))
    else:
        print(json.dumps({{"status": "PASS", "reasoning": "ok"}}))
else:
    import os
    Path("output.txt").write_text("done\\n")
    sys.exit(0)
""")

    cfg = {
        "agent_command": f"{sys.executable} {wrapper} --model {{model}}",
        "agent_timeout_seconds": 10,
        "verification_timeout_seconds": 10,
        "max_retries": 1,
    }
    (project_root / "agent-build.config.json").write_text(json.dumps(cfg))

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 0, result.stderr
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "completed"


def test_verification_exhausted_retries(project_root):
    """Verification always FAILs and retries are exhausted → failed record."""
    make_agent_config(
        project_root,
        create_file="output.txt",
        verification_fail=True,
        fail_reason="Always fails",
        max_retries=1,
    )

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "failed"
    assert "Verification" in result.stderr or "retry limit" in result.stderr


def test_nonzero_agent_no_verification(project_root):
    """Agent exits non-zero → failed record, verifications NOT run."""
    # If verification were run, it would create a marker file.
    make_agent_config(
        project_root,
        exit_code=1,
        verification_create_file="verif_ran.txt",
    )

    result = run_cli(project_root, "run", "--yes")

    assert result.returncode == 1
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "failed"
    # Marker file must NOT exist — verifications were skipped
    assert not (project_root / "src" / "verif_ran.txt").exists()


# ─────────────────────────────────────────────────────────────────────────────
# --skip-build tests
# ─────────────────────────────────────────────────────────────────────────────

def test_skip_build_happy_path(project_root):
    """--skip-build: agent not invoked, verifications run, completed record written, commit created."""
    # If the agent were invoked, it would create agent_ran.txt.
    make_agent_config(project_root, create_file="agent_ran.txt")

    result = run_cli(project_root, "run", "--skip-build", "--yes")

    assert result.returncode == 0, result.stderr
    assert "Skipping build step" in result.stdout
    assert "Invoking agent" not in result.stdout

    # Agent must NOT have run
    assert not (project_root / "src" / "agent_ran.txt").exists()

    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "completed"

    # A new commit must exist
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=project_root, capture_output=True, text=True, check=True,
    )
    assert len(log.stdout.strip().splitlines()) >= 2


def test_skip_build_failing_verification(project_root):
    """--skip-build with a failing verification triggers the retry loop."""
    import sys
    # Stateful fake: first verification FAIL, second PASS
    count_file = str(project_root / ".skip-build-count")
    wrapper = project_root / "skip_verif.py"
    wrapper.write_text(f"""\
#!/usr/bin/env python3
import json, sys
from pathlib import Path

count_file = Path({count_file!r})
n = int(count_file.read_text()) if count_file.exists() else 0
n += 1
count_file.write_text(str(n))

stdin = sys.stdin.read()
is_verif = "Respond with a JSON object on the last line" in stdin

if is_verif:
    if n == 1:
        print(json.dumps({{"status": "FAIL", "reasoning": "initial failure"}}))
    else:
        print(json.dumps({{"status": "PASS", "reasoning": "ok"}}))
else:
    sys.exit(0)
""")

    cfg = {
        "agent_command": f"{sys.executable} {wrapper} --model {{model}}",
        "agent_timeout_seconds": 10,
        "verification_timeout_seconds": 10,
        "max_retries": 1,
    }
    (project_root / "agent-build.config.json").write_text(json.dumps(cfg))

    result = run_cli(project_root, "run", "--skip-build", "--yes")

    assert result.returncode == 0, result.stderr
    # "Skipping build step" should appear at least twice (initial + one retry)
    assert result.stdout.count("Skipping build step") >= 2
    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "completed"


def test_skip_build_yes_no_prompts(project_root):
    """--skip-build --yes completes without hanging on any prompts."""
    make_agent_config(project_root)

    result = run_cli(project_root, "run", "--skip-build", "--yes")

    assert result.returncode == 0


def test_skip_build_precommitted_fix(project_root):
    """
    Pre-committed fix scenario:
    1. Inject a failed record for task 001-first.
    2. Commit a manual file change to src/.
    3. Run --skip-build → tool succeeds, new commit created.
    4. The new commit's diff must contain only results/ changes (no src/ changes).
    """
    # Inject a failed record
    write_result(project_root, "001-first", "failed")
    make_agent_config(project_root)

    # Pre-populate src/.gitignore so workspace prep doesn't add it as a new
    # src/ change in the tool's commit (it would otherwise count as a src/ diff).
    gitignore = project_root / "src" / ".gitignore"
    gitignore.write_text(".agent-context\n")
    subprocess.run(["git", "add", "src/.gitignore"],
                   cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add gitignore"],
                   cwd=project_root, check=True, capture_output=True)

    # Simulate user manually fixing src/ and committing
    (project_root / "src" / "manual_fix.txt").write_text("manual fix\n")
    subprocess.run(["git", "add", "src/manual_fix.txt"],
                   cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "manual fix"],
                   cwd=project_root, check=True, capture_output=True)

    result = run_cli(project_root, "run", "--skip-build", "--yes")

    assert result.returncode == 0, result.stderr

    record = json.loads(
        (project_root / "results" / "results-001-first.json").read_text()
    )
    assert record["status"] == "completed"

    # The most recent commit's diff must NOT include src/ changes
    src_diff = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--", "src/"],
        cwd=project_root, capture_output=True, text=True, check=True,
    )
    assert src_diff.stdout.strip() == "", (
        "Expected no src/ changes in the --skip-build commit, "
        f"but got: {src_diff.stdout}"
    )

    # The results/ must have changed in that commit
    results_diff = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--", "results/"],
        cwd=project_root, capture_output=True, text=True, check=True,
    )
    assert results_diff.stdout.strip() != "", "Expected results/ changes in the commit"
