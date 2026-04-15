from __future__ import annotations

import json
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import Config
from .events import (
    AgentCompleted,
    AgentOutput,
    AgentStarted,
    AgentTimedOut,
    EventEmitter,
)


class AgentOutcome(str, Enum):
    COMPLETED = "completed"  # exited 0
    FAILED = "failed"  # exited non-zero
    TIMED_OUT = "timed_out"  # killed after timeout
    LAUNCH_ERROR = "launch_error"  # OSError on Popen


@dataclass
class AgentResult:
    outcome: AgentOutcome
    exit_code: Optional[int] = None
    error: Optional[Exception] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost: Optional[float] = None
    session_id: Optional[str] = None


class OpenCodeParser:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.session_id: Optional[str] = None

    def parse_line(self, line: str) -> Optional[str]:
        """Parse a JSON line and return a human-readable string if applicable."""
        line = line.strip()
        if not line:
            return None

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            # If it's not valid JSON, just pass it through as raw text
            return line

        event_type = data.get("type")
        part = data.get("part", {})

        if event_type == "session":
            self.session_id = data.get("session_id")
        elif event_type == "text":
            return part.get("text")
        elif event_type == "tool_use":
            tool_name = part.get("tool", "unknown_tool")
            return f"\n> Using tool: {tool_name}\n"
        elif event_type == "step_finish":
            tokens = part.get("tokens", {})
            self.input_tokens += tokens.get("input", 0)
            self.output_tokens += tokens.get("output", 0)
            self.cost += part.get("cost", 0.0)
            # Some agents might provide session_id in step_finish
            if not self.session_id:
                self.session_id = data.get("session_id")

        return None


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
        parts.extend(
            [
                "",
                "Global instructions that apply to all tasks are in"
                " `.agent-context/global/GLOBAL.md`.",
                "Read these before beginning work.",
            ]
        )

    parts.extend(
        [
            "",
            "Verification checks that will be run against your output are defined as files in",
            "`.agent-context/verifications/`. You MAY review them to understand what success"
            " looks like.",
            "",
            "Complete the task. When you are done, stop. Verifications will be run automatically.",
        ]
    )

    return "\n".join(parts)


def run_agent(
    project_root: Path,
    config: Config,
    prompt: str,
    emitter: EventEmitter,
    session_id: Optional[str] = None,
) -> AgentResult:
    """
    Launch the agent as a subprocess with the given *prompt* via argv.

    - cwd is set to <project_root>/src/
    - stdout/stderr are captured line by line to support streaming json
    - stdin is mapped to DEVNULL
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
    parser = OpenCodeParser()

    try:
        try:
            argv = [
                arg.replace("{prompt}", prompt).replace(
                    "{session_id}", session_id or ""
                )
                for arg in config.agent_argv
            ]
            process = subprocess.Popen(
                argv,
                cwd=src_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # Line buffered
            )
        except OSError as exc:
            return AgentResult(outcome=AgentOutcome.LAUNCH_ERROR, error=exc)

        emitter.emit(AgentStarted())

        import threading

        def output_reader():
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                human_text = parser.parse_line(line)
                if human_text:
                    emitter.emit(AgentOutput(chunk=human_text))

        reader_thread = threading.Thread(target=output_reader, daemon=True)
        reader_thread.start()

        try:
            process.wait(timeout=config.agent_timeout_seconds)
            reader_thread.join(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            emitter.emit(AgentTimedOut())
            return AgentResult(outcome=AgentOutcome.TIMED_OUT)
        except KeyboardInterrupt:
            process.kill()
            process.wait()
            raise

        emitter.emit(AgentCompleted(exit_code=process.returncode))

        if process.returncode == 0:
            return AgentResult(
                outcome=AgentOutcome.COMPLETED,
                exit_code=0,
                input_tokens=parser.input_tokens,
                output_tokens=parser.output_tokens,
                cost=parser.cost,
                session_id=parser.session_id,
            )
        return AgentResult(
            outcome=AgentOutcome.FAILED,
            exit_code=process.returncode,
            input_tokens=parser.input_tokens,
            output_tokens=parser.output_tokens,
            cost=parser.cost,
            session_id=parser.session_id,
        )

    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
