from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .agent import AgentOutcome, run_agent
from .config import Config
from .events import EventEmitter
from .types import AgentInvocation


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


def _run_single_verification(
    verification_file: Path,
    project_root: Path,
    config: Config,
    emitter: EventEmitter,
) -> tuple[VerificationResult, AgentInvocation]:
    """
    Run a single verification agent for *verification_file*.

    Returns a tuple of (VerificationResult, AgentInvocation).
    All error conditions (OSError, timeout, non-zero exit, empty/non-JSON output)
    produce a synthetic FAIL; parse exceptions are never propagated.
    """
    verification_id = verification_file.stem

    invocation = AgentInvocation(
        type="verification",
        model=config.model,
        verification_id=verification_id,
    )

    try:
        file_content = verification_file.read_text(encoding="utf-8")
    except OSError as exc:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning=f"Verification file could not be read: {exc}",
            verification_id=verification_id,
        ), invocation

    prompt = _build_verification_prompt(file_content)

    # Note: run_agent handles timeout, process management, and sigterm.
    # It also emits events on the supplied emitter.
    verification_config = Config(
        agent_command=config.agent_command,
        agent_resume_command=config.agent_resume_command,
        model=config.model,
        agent_timeout_seconds=config.verification_timeout_seconds,
        verification_timeout_seconds=config.verification_timeout_seconds,
        max_retries=config.max_retries,
    )
    agent_result = run_agent(
        project_root=project_root,
        config=verification_config,
        prompt=prompt,
        emitter=emitter,
    )

    invocation.input_tokens = agent_result.input_tokens
    invocation.output_tokens = agent_result.output_tokens
    invocation.cost = agent_result.cost

    if agent_result.outcome == AgentOutcome.LAUNCH_ERROR:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning=f"Verification could not be launched: {agent_result.error}",
            verification_id=verification_id,
        ), invocation

    if agent_result.outcome == AgentOutcome.TIMED_OUT:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning=(
                f"Verification timed out after "
                f"{config.verification_timeout_seconds} seconds."
            ),
            verification_id=verification_id,
            timed_out=True,
        ), invocation

    if agent_result.outcome == AgentOutcome.FAILED:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning=f"Verification exited with non-zero exit code {agent_result.exit_code}.",
            verification_id=verification_id,
        ), invocation

    # Parse last_json returned by agent
    data = agent_result.last_json

    if data is None:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning="Verification produced no valid JSON output or could not be parsed.",
            verification_id=verification_id,
        ), invocation

    try:
        raw_status = data["status"]
    except (KeyError, TypeError):
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning="Verification response missing 'status' field.",
            verification_id=verification_id,
        ), invocation

    if raw_status == "PASS":
        return VerificationResult(
            status=VerificationStatus.PASS,
            reasoning=data.get("reasoning") or "",
            verification_id=verification_id,
        ), invocation
    elif raw_status == "FAIL":
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning=data.get("reasoning") or "",
            verification_id=verification_id,
        ), invocation
    else:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            reasoning=f"Verification returned unknown status: {raw_status!r}.",
            verification_id=verification_id,
        ), invocation


def run_verifications(
    project_root: Path,
    config: Config,
    emitter: Optional[EventEmitter] = None,
) -> tuple[VerificationStatus, Optional[VerificationResult], list[AgentInvocation]]:
    """
    Run all verification agents found in src/.agent-context/verifications/
    in lexicographic filename order.  Halts on the first FAIL.

    Returns:
      (PASS, None, invocations)    — all verifications passed, or no files found
      (FAIL, result, invocations)  — first failing VerificationResult
    """
    verifications_dir = project_root / "src" / ".agent-context" / "verifications"
    invocations: list[AgentInvocation] = []
    effective_emitter = emitter or EventEmitter()

    if not verifications_dir.exists():
        return VerificationStatus.PASS, None, invocations

    verification_files = sorted(
        [f for f in verifications_dir.iterdir() if f.is_file()],
        key=lambda f: f.name,
    )

    if not verification_files:
        return VerificationStatus.PASS, None, invocations

    for vfile in verification_files:
        result, invocation = _run_single_verification(
            vfile, project_root, config, effective_emitter
        )
        invocations.append(invocation)
        if result.status == VerificationStatus.FAIL:
            return VerificationStatus.FAIL, result, invocations

    return VerificationStatus.PASS, None, invocations
