from __future__ import annotations

import logging
import re
from pathlib import Path

from .types import Task

logger = logging.getLogger(__name__)

_LEADING_ALPHANUM_RE = re.compile(r"^[a-zA-Z0-9]")


class ProjectError(Exception):
    pass


def load_tasks(project_root: Path) -> list[Task]:
    """
    Load and return tasks sorted lexicographically by directory name.

    Raises ProjectError for structural problems:
      - tasks/ directory absent
      - tasks/ directory exists but contains no task subdirectories
      - any task directory is missing TASK.md
    Emits a WARNING for task names without a leading alphanumeric prefix.
    """
    tasks_dir = project_root / "tasks"

    if not tasks_dir.exists():
        raise ProjectError("tasks/ directory does not exist")

    subdirs = sorted(
        [d for d in tasks_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )

    if not subdirs:
        raise ProjectError("tasks/ directory exists but contains no task subdirectories")

    tasks: list[Task] = []
    for subdir in subdirs:
        task_md = subdir / "TASK.md"
        if not task_md.exists():
            raise ProjectError(
                f"Task directory '{subdir.name}' is missing TASK.md"
            )
        if not _LEADING_ALPHANUM_RE.match(subdir.name):
            logger.warning(
                "Task directory '%s' does not begin with an alphanumeric prefix; "
                "ordering may be ambiguous",
                subdir.name,
            )
        tasks.append(Task(id=subdir.name, path=subdir))

    return tasks
