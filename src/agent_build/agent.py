from __future__ import annotations

import signal
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import Config
from .events import AgentCompleted, AgentOutput, AgentStarted, AgentTimedOut, EventEmitter


class AgentOutcome(str, Enum):
    COMPLETED = "completed"       # exited 0
    FAILED = "failed"             # exited non-zero
    TIMED_OUT = "timed_out"       # killed after timeout
    LAUNCH_ERROR = "launch_error" # OSError on Popen


@dataclass
class AgentResult:
    outcome: AgentOutcome
    exit_code: Optional[int] = None
    error: Optional[Exception] = None


def build_prompt(project_root: Path) -> str:
    """
    Construct the agent prompt.  The global-instructions paragraph is included
    only when src/.agent-context/global/GLOBAL.md was copied by workspace prep.
    """
    parts = [
        "You are a coding agent.",
        "",
        "Your task instructions are in `.agent-context/task/TASK.md`. Read them before doing",
        "anything else. The file may reference additional files within `.agent-context/task/`",
        "for progressive disclosure — load them as needed.",
    ]

    global_md = project_root / "src" / ".agent-context" / "global" / "GLOBAL.md"
    if global_md.exists():
        parts.extend([
            "",
            "Global instructions that apply to all tasks are in"
            " `.agent-context/global/GLOBAL.md`.",
            "Read these before beginning work.",
        ])

    parts.extend([
        "",
        "Verification checks that will be run against your output are defined as files in",
        "`.agent-context/verifications/`. You MAY review them to understand what success"
        " looks like.",
        "",
        "Complete the task. When you are done, stop. Verifications will be run automatically.",
    ])

    return "\n".join(parts)


def run_agent(
    project_root: Path,
    config: Config,
    prompt: str,
    emitter: EventEmitter,
) -> AgentResult:
    """
    Launch the agent as a subprocess with the given *prompt* via stdin.

    - cwd is set to <project_root>/src/
    - stdout/stderr are captured; no TTY is allocated
    - A SIGTERM handler is installed for the duration of the call; on SIGTERM
      the subprocess is killed, reaped, and SystemExit(1) is raised so that
      enclosing finally blocks (e.g. lock release) execute normally.
    - SIGINT (KeyboardInterrupt) kills and reaps the subprocess then re-raises.
    - On timeout the subprocess is killed, reaped, and AgentTimedOut is emitted.
    - OSError on launch → AgentResult(LAUNCH_ERROR) with no retry.
    """
    src_dir = project_root / "src"
    process: Optional[subprocess.Popen[str]] = None

    def _sigterm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        if process is not None:
            process.kill()
            process.wait()
        raise SystemExit(1)

    old_sigterm = signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        try:
            process = subprocess.Popen(
                config.agent_argv,
                cwd=src_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            return AgentResult(outcome=AgentOutcome.LAUNCH_ERROR, error=exc)

        emitter.emit(AgentStarted())

        try:
            stdout, _ = process.communicate(
                input=prompt,
                timeout=config.agent_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            emitter.emit(AgentTimedOut())
            return AgentResult(outcome=AgentOutcome.TIMED_OUT)
        except KeyboardInterrupt:
            process.kill()
            process.wait()
            raise

        if stdout:
            emitter.emit(AgentOutput(chunk=stdout))
        emitter.emit(AgentCompleted(exit_code=process.returncode))

        if process.returncode == 0:
            return AgentResult(outcome=AgentOutcome.COMPLETED, exit_code=0)
        return AgentResult(outcome=AgentOutcome.FAILED, exit_code=process.returncode)

    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
