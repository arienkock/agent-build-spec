from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONFIG_FILENAME = "agent-build.config.json"

DEFAULTS: dict = {
    "agent_command": "opencode run {prompt} --model {model} --dangerously-skip-permissions --format json",
    "agent_resume_command": "opencode run {prompt} --session {session_id} --model {model} --dangerously-skip-permissions --format json",
    "model": "openrouter/google/gemini-2.5-pro",
    "agent_timeout_seconds": 600,
    "verification_timeout_seconds": 120,
    "max_retries": 3,
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    agent_command: str
    agent_resume_command: str
    model: str
    agent_timeout_seconds: int
    verification_timeout_seconds: int
    max_retries: int

    @property
    def agent_argv(self) -> list[str]:
        """Return the agent command as an argv list with {model} substituted."""
        return shlex.split(self.agent_command.replace("{model}", self.model))

    @property
    def agent_resume_argv(self) -> list[str]:
        """Return the agent resume command as an argv list with {model} substituted."""
        return shlex.split(self.agent_resume_command.replace("{model}", self.model))


def _validate_command(name: str, command: str, model: str) -> None:
    if not command.strip():
        raise ConfigError(f"{name} must not be empty")

    # Substitute {model} and {prompt} and check for leftover format fields
    substituted = (
        command.replace("{model}", model)
        .replace("{prompt}", "")
        .replace("{session_id}", "")
    )

    # Detect any remaining {…} placeholders
    remaining = re.findall(r"\{[^}]*\}", substituted)
    if remaining:
        raise ConfigError(
            f"{name} contains unsupported placeholder(s): {', '.join(remaining)}. "
            "Only {model}, {prompt}, and {session_id} are allowed."
        )


def _validate_timeout(name: str, value: int) -> None:
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {value}")


def load_config(project_root: Path) -> Config:
    """Load config from agent-build.config.json; missing fields use defaults."""
    config_path = project_root / CONFIG_FILENAME
    raw: dict = {}

    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Failed to parse {CONFIG_FILENAME}: {exc}") from exc

    agent_command = str(raw.get("agent_command", DEFAULTS["agent_command"]))
    agent_resume_command = str(
        raw.get("agent_resume_command", DEFAULTS["agent_resume_command"])
    )
    model = str(raw.get("model", DEFAULTS["model"]))
    agent_timeout = int(
        raw.get("agent_timeout_seconds", DEFAULTS["agent_timeout_seconds"])
    )
    verification_timeout = int(
        raw.get(
            "verification_timeout_seconds", DEFAULTS["verification_timeout_seconds"]
        )
    )
    max_retries = int(raw.get("max_retries", DEFAULTS["max_retries"]))

    _validate_command("agent_command", agent_command, model)
    _validate_command("agent_resume_command", agent_resume_command, model)
    _validate_timeout("agent_timeout_seconds", agent_timeout)
    _validate_timeout("verification_timeout_seconds", verification_timeout)

    return Config(
        agent_command=agent_command,
        agent_resume_command=agent_resume_command,
        model=model,
        agent_timeout_seconds=agent_timeout,
        verification_timeout_seconds=verification_timeout,
        max_retries=max_retries,
    )
