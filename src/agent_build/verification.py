from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import Config


class VerificationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass
class VerificationResult:
    status: VerificationStatus
    reasoning: str
    verification_id: str  # filename stem, e.g. "001-check"
    timed_out: bool = False


def _build_verification_prompt(file_content: str) -> str:
    """
    Construct the verification agent prompt.

    The prompt is the verbatim file content followed by a task reference and
    a JSON-response instruction.  The sentinel string
    "Respond with a JSON object on the last line" is used by fake_agent.py in
    tests to detect verification calls.
    """
    return (
        f"{file_content}\n\n"
        "---\n\n"
        "Task instructions are in `.agent-context/task/TASK.md`.\n\n"
        "Respond with a JSON object on the last line: "
        '{ "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }'
    )


def _parse_last_json_line(stdout: str) -> Optional[dict]:
    """Return the parsed JSON from the last non-empty line, or None on failure."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except (json.JSONDecodeError, ValueError):
        return None


def _run_single_verification(
    verification_file: Path,
    project_root: Path,
    config: Config,
) -> VerificationResult:
    """
    Run a single verification agent for *verification_file*.

    Returns a VerificationResult with PASS or FAIL status.
    All error conditions (OSError, timeout, non-zero exit, empty/non-JSON output)
    produce a synthetic FAIL; parse exceptions are never propagated.
    SIGINT kills the subprocess and re-raises so the outer finally block in
    task_run.py releases the lock.
    """
    verification_id = verification_file.stem
    src_dir = project_root / "src"

    try:
        file_content = verification_file.read_text(encoding="utf-8")
    except OSError as exc:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning=f"Verification file could not be read: {exc}",
            verification_id=verification_id,
        )

    prompt = _build_verification_prompt(file_content)

    process: Optional[subprocess.Popen[str]] = None

    try:
        try:
            argv = [arg.replace("{prompt}", prompt) for arg in config.agent_argv]
            process = subprocess.Popen(
                argv,
                cwd=src_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                reasoning=f"Verification could not be launched: {exc}",
                verification_id=verification_id,
            )

        try:
            stdout, _ = process.communicate(
                timeout=config.verification_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return VerificationResult(
                status=VerificationStatus.FAIL,
                reasoning=(
                    f"Verification timed out after "
                    f"{config.verification_timeout_seconds} seconds."
                ),
                verification_id=verification_id,
                timed_out=True,
            )
        except KeyboardInterrupt:
            process.kill()
            process.wait()
            raise

        if process.returncode != 0:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                reasoning=(
                    f"Verification exited with non-zero exit code {process.returncode}."
                ),
                verification_id=verification_id,
            )

        data = _parse_last_json_line(stdout)

        if data is None:
            lines = [line for line in stdout.splitlines() if line.strip()]
            if not lines:
                return VerificationResult(
                    status=VerificationStatus.FAIL,
                    reasoning="Verification produced no output.",
                    verification_id=verification_id,
                )
            return VerificationResult(
                status=VerificationStatus.FAIL,
                reasoning="Verification output could not be parsed as JSON.",
                verification_id=verification_id,
            )

        try:
            raw_status = data["status"]
        except (KeyError, TypeError):
            return VerificationResult(
                status=VerificationStatus.FAIL,
                reasoning="Verification response missing 'status' field.",
                verification_id=verification_id,
            )

        if raw_status == "PASS":
            return VerificationResult(
                status=VerificationStatus.PASS,
                reasoning=data.get("reasoning") or "",
                verification_id=verification_id,
            )
        elif raw_status == "FAIL":
            return VerificationResult(
                status=VerificationStatus.FAIL,
                reasoning=data.get("reasoning") or "",
                verification_id=verification_id,
            )
        else:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                reasoning=f"Verification returned unknown status: {raw_status!r}.",
                verification_id=verification_id,
            )

    finally:
        pass  # No SIGTERM handler; outer task_run finally handles lock release.


def run_verifications(
    project_root: Path,
    config: Config,
) -> tuple[VerificationStatus, Optional[VerificationResult]]:
    """
    Run all verification agents found in src/.agent-context/verifications/
    in lexicographic filename order.  Halts on the first FAIL.

    Returns:
      (PASS, None)          — all verifications passed, or no files found
      (FAIL, result)        — first failing VerificationResult
    """
    verifications_dir = project_root / "src" / ".agent-context" / "verifications"

    if not verifications_dir.exists():
        return VerificationStatus.PASS, None

    verification_files = sorted(
        [f for f in verifications_dir.iterdir() if f.is_file()],
        key=lambda f: f.name,
    )

    if not verification_files:
        return VerificationStatus.PASS, None

    for vfile in verification_files:
        result = _run_single_verification(vfile, project_root, config)
        if result.status == VerificationStatus.FAIL:
            return VerificationStatus.FAIL, result

    return VerificationStatus.PASS, None
