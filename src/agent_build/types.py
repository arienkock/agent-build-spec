from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class TaskRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Task:
    id: str
    path: Path


@dataclass
class AgentInvocation:
    type: str  # "implementation", "review", "verification"
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost: Optional[float] = None
    verification_id: Optional[str] = None


@dataclass
class ResultRecord:
    status: TaskRunStatus
    previous_results: Optional[str]  # archived filename or None
    base_commit: Optional[str] = None
    start_time: Optional[str] = None  # ISO 8601
    end_time: Optional[str] = None
    cpu_user_time: Optional[float] = None
    cpu_system_time: Optional[float] = None
    io_time: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost: Optional[float] = None
    invocations: list[AgentInvocation] = field(default_factory=list)


class ResumePointKind(str, Enum):
    READY = "ready"
    COMPLETE = "complete"
    NEEDS_CONFIRMATION = "needs_confirmation"  # running task found
    ERROR = "error"  # ambiguous state


@dataclass
class ResumePoint:
    kind: ResumePointKind
    task: Optional[Task] = None
    message: Optional[str] = None
