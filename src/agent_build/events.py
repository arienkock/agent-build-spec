from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union


@dataclass
class AgentStarted:
    """Emitted immediately before the agent subprocess begins processing."""


@dataclass
class AgentOutput:
    """Emitted as output chunks arrive from the agent."""
    chunk: str


@dataclass
class AgentCompleted:
    """Emitted when the agent subprocess exits normally."""
    exit_code: int


@dataclass
class AgentTimedOut:
    """Emitted when the agent subprocess is killed due to timeout."""


AgentEvent = Union[AgentStarted, AgentOutput, AgentCompleted, AgentTimedOut]


class EventEmitter:
    """Simple typed event emitter for agent lifecycle events."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[AgentEvent], None]] = []

    def subscribe(self, listener: Callable[[AgentEvent], None]) -> None:
        self._listeners.append(listener)

    def emit(self, event: AgentEvent) -> None:
        for listener in self._listeners:
            listener(event)
