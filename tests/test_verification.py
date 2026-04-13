"""
Unit tests for verification.py.

These tests call run_verifications() directly (not via CLI) with a real Config
pointing at fake_agent.py.  The fake_agent detects verification calls by
checking stdin for the sentinel string.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_build.config import Config
from agent_build.verification import (
    VerificationResult,
    VerificationStatus,
    _run_single_verification,
    run_verifications,
)

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


def _make_config(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    verification_fail: bool = False,
    fail_reason: str = "Test failure",
    verification_exit_code: int = 0,
    verification_no_output: bool = False,
    verification_raw_output: str | None = None,
    verification_sleep: float = 0,
    verification_create_file: str | None = None,
    fail_if_stdin_contains: str | None = None,
    invocation_count_file: str | None = None,
    verification_timeout_seconds: int = 10,
) -> Config:
    import shlex
    cmd = f"{sys.executable} {FAKE_AGENT} --exit-code {exit_code} --model {{model}}"
    if verification_fail:
        cmd += " --verification-fail"
    if fail_reason != "Test failure":
        cmd += f" --fail-reason {shlex.quote(fail_reason)}"
    if verification_exit_code != 0:
        cmd += f" --verification-exit-code {verification_exit_code}"
    if verification_no_output:
        cmd += " --verification-no-output"
    if verification_raw_output is not None:
        cmd += f" --verification-raw-output {shlex.quote(verification_raw_output)}"
    if verification_sleep > 0:
        cmd += f" --verification-sleep {verification_sleep}"
    if verification_create_file is not None:
        cmd += f" --verification-create-file {shlex.quote(verification_create_file)}"
    if fail_if_stdin_contains is not None:
        cmd += f" --fail-if-stdin-contains {shlex.quote(fail_if_stdin_contains)}"
    if invocation_count_file is not None:
        cmd += f" --invocation-count-file {shlex.quote(invocation_count_file)}"
    # Construct Config directly (bypasses config-file validation) so that
    # raw output strings containing '{...}' don't trip the placeholder check.
    return Config(
        agent_command=cmd,
        model="test-model",
        agent_timeout_seconds=10,
        verification_timeout_seconds=verification_timeout_seconds,
        max_retries=0,
    )


def _setup_project(tmp_path: Path) -> Path:
    """Create a minimal project layout with src/.agent-context/verifications/."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "src").mkdir()
    vdir = project_root / "src" / ".agent-context" / "verifications"
    vdir.mkdir(parents=True)
    # Also create task dir so verification prompt reference is valid
    tdir = project_root / "src" / ".agent-context" / "task"
    tdir.mkdir(parents=True)
    (tdir / "TASK.md").write_text("# Task\nDo something.\n")
    return project_root


def _add_verification(verifications_dir: Path, name: str, content: str = "# Check\n") -> Path:
    vf = verifications_dir / name
    vf.write_text(content)
    return vf


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_pass_response(tmp_path):
    """Fake agent returns PASS JSON → VerificationStatus.PASS."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    config = _make_config(tmp_path)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.PASS
    assert result is None


def test_last_nonempty_line_parsed(tmp_path):
    """Only the last non-empty line of stdout is parsed as JSON."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")

    # Build a fake agent that outputs extra lines followed by the JSON
    import shlex
    raw_output = "some preamble\nmore text\n" + json.dumps({"status": "PASS", "reasoning": "ok"})
    config = _make_config(tmp_path, verification_raw_output=raw_output)

    # raw_output ends with JSON, but _make_config uses --verification-raw-output
    # which prints the raw string; the last non-empty line is the JSON.
    # However, the raw_output itself IS the JSON line when passed as --verification-raw-output
    # (since it's printed as-is). We need the last line to be JSON.
    # The raw_output we constructed has the JSON as the last line.
    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.PASS


def test_nonzero_exit_fail(tmp_path):
    """Non-zero exit code → FAIL with synthetic reasoning."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    config = _make_config(tmp_path, verification_exit_code=1)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert "non-zero exit code" in result.reasoning


def test_empty_output_fail(tmp_path):
    """Empty output → FAIL with synthetic reasoning."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    config = _make_config(tmp_path, verification_no_output=True)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert "no output" in result.reasoning.lower()


def test_nonjson_output_fail(tmp_path):
    """Non-JSON output → FAIL with synthetic reasoning."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    config = _make_config(tmp_path, verification_raw_output="this is not json at all")

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert "json" in result.reasoning.lower()


def test_timeout_fail(tmp_path):
    """Subprocess that exceeds timeout → FAIL with synthetic reasoning."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    # Sleep longer than the 1-second timeout
    config = _make_config(tmp_path, verification_sleep=3, verification_timeout_seconds=1)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert "timed out" in result.reasoning.lower()


def test_oserror_fail(tmp_path):
    """OSError on launch → FAIL with synthetic reasoning."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")

    # Use a bogus command that cannot be launched
    bad_config = Config(
        agent_command="/nonexistent/binary/agent --model {model}",
        model="test",
        agent_timeout_seconds=10,
        verification_timeout_seconds=10,
        max_retries=0,
    )

    status, result = run_verifications(project_root, bad_config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert "could not be launched" in result.reasoning.lower()


def test_no_files_skip(tmp_path):
    """Empty verifications directory → PASS, None (no verification run)."""
    project_root = _setup_project(tmp_path)
    # verifications dir is empty
    config = _make_config(tmp_path)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.PASS
    assert result is None


def test_no_verifications_dir(tmp_path):
    """Missing verifications directory → PASS, None."""
    project_root = _setup_project(tmp_path)
    # Remove the verifications dir
    import shutil
    shutil.rmtree(project_root / "src" / ".agent-context" / "verifications")
    config = _make_config(tmp_path)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.PASS
    assert result is None


def test_lexicographic_halt_on_first_fail(tmp_path):
    """
    Two verification files: aaa.md and bbb.md, both configured to FAIL.
    Since 'aaa.md' sorts first, the returned verification_id must be 'aaa'.
    """
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "aaa.md", "# Check A\n")
    _add_verification(vdir, "bbb.md", "# Check B\n")
    config = _make_config(tmp_path, verification_fail=True)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert result.verification_id == "aaa"


def test_verification_id_is_stem(tmp_path):
    """verification_id is the filename stem (without extension)."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "007-syntax.md")
    config = _make_config(tmp_path, verification_fail=True)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert result.verification_id == "007-syntax"


def test_extra_json_fields_pass(tmp_path):
    """JSON with extra fields alongside valid status: PASS → valid PASS."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    # Output JSON with extra fields
    raw_output = json.dumps({"status": "PASS", "reasoning": "ok", "extra": "ignored", "score": 99})
    config = _make_config(tmp_path, verification_raw_output=raw_output)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.PASS


def test_second_fail_not_accumulated(tmp_path):
    """
    Two verification files: aaa.md (PASS), bbb.md (FAIL).
    The returned result must reference bbb, not aaa.
    """
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    # aaa.md has unique content; fake agent PASSes unless stdin contains "Check B"
    _add_verification(vdir, "aaa.md", "# Check A\nAll good here.\n")
    _add_verification(vdir, "bbb.md", "# Check B\nThis will fail.\n")
    # Configure fake to FAIL only when stdin contains the bbb.md content marker
    config = _make_config(tmp_path, fail_if_stdin_contains="Check B")

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert result.verification_id == "bbb"


def test_fail_result_contains_reasoning(tmp_path):
    """FAIL response includes the reasoning from the agent."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    config = _make_config(tmp_path, verification_fail=True, fail_reason="Missing required field")

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert result.reasoning == "Missing required field"


def test_verification_cwd_is_src(tmp_path):
    """Verification subprocess cwd is project_root/src/."""
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    marker = "cwd_marker.txt"
    config = _make_config(tmp_path, verification_create_file=marker)

    run_verifications(project_root, config)

    # The file should have been created relative to src/
    assert (project_root / "src" / marker).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Edge case tests (partial-state / mid-run failures)
# ─────────────────────────────────────────────────────────────────────────────

def test_verification_file_deleted_mid_run(tmp_path):
    """
    If a verification file is deleted between directory listing and reading
    (simulated by passing a path to a non-existent file), run_verifications
    must return FAIL with a clear message instead of raising FileNotFoundError.
    """
    project_root = _setup_project(tmp_path)
    config = _make_config(tmp_path)

    # Point directly at a file that doesn't exist — simulates the race condition
    # where the file existed during iterdir() but was removed before read_text().
    missing_file = project_root / "src" / ".agent-context" / "verifications" / "gone.md"

    result = _run_single_verification(missing_file, project_root, config)

    assert result.status == VerificationStatus.FAIL
    assert "could not be read" in result.reasoning.lower()


def test_null_reasoning_coerced_to_empty_string(tmp_path):
    """
    When the verification JSON has "reasoning": null, the VerificationResult
    reasoning must be an empty string, not None (which would print 'None').
    """
    project_root = _setup_project(tmp_path)
    vdir = project_root / "src" / ".agent-context" / "verifications"
    _add_verification(vdir, "001-check.md")
    # Return JSON with explicit null reasoning
    null_reasoning_json = json.dumps({"status": "FAIL", "reasoning": None})
    config = _make_config(tmp_path, verification_raw_output=null_reasoning_json)

    status, result = run_verifications(project_root, config)

    assert status == VerificationStatus.FAIL
    assert result is not None
    assert result.reasoning == ""  # null coerced to empty string, not "None"
