from __future__ import annotations

import shutil
from pathlib import Path

import click

from .types import Task


class WorkspaceError(Exception):
    pass


def _ensure_gitignore_entry(src_dir: Path) -> None:
    """
    Ensure `.agent-context` appears as a non-negated, non-comment line in
    src/.gitignore. Creates the file if absent. Appends the entry if needed.

    Exact line match rules (per spec):
    - Strip trailing whitespace before comparing.
    - Lines starting with `#` (comments) are ignored.
    - Lines starting with `!` (negations) are ignored — even `!.agent-context`
      does NOT satisfy the requirement; we still append the non-negated line.
    - `.agent-context/` (with trailing slash) is NOT an exact match; we still
      append the bare `.agent-context`.
    """
    gitignore_path = src_dir / ".gitignore"

    existing_lines: list[str] = []
    if gitignore_path.exists():
        existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()

    for line in existing_lines:
        stripped = line.rstrip()
        if stripped.startswith("#") or stripped.startswith("!"):
            continue
        if stripped == ".agent-context":
            return  # Already present as a non-negated entry

    # Append the entry; add a leading newline only if the file exists and
    # doesn't already end with one (checked via raw bytes to avoid splitlines()
    # stripping the trailing newline).
    raw = gitignore_path.read_bytes() if gitignore_path.exists() else b""
    with gitignore_path.open("a", encoding="utf-8") as fh:
        if raw and not raw.endswith(b"\n"):
            fh.write("\n")
        fh.write(".agent-context\n")


def prepare(project_root: Path, task: Task) -> None:
    """
    Prepare the agent workspace for *task* inside src/.agent-context/.

    Steps:
    1. Prompt and remove any existing .agent-context/ (previous run artifact).
    2. Ensure .agent-context is in src/.gitignore (create/append if needed).
    3. Copy task dir  → src/.agent-context/task/
    4. Copy global/   → src/.agent-context/global/  (skipped if absent)
    5. Copy verifications/ → src/.agent-context/verifications/
    """
    src_dir = project_root / "src"
    agent_context = src_dir / ".agent-context"

    # Prompt before overwriting an existing .agent-context/
    if agent_context.exists():
        click.echo(
            "Warning: src/.agent-context/ already exists. "
            "A previous run likely left it behind."
        )
        if not click.confirm("Delete it and recopy?"):
            raise WorkspaceError(
                "Aborted: existing src/.agent-context/ was not overwritten."
            )
        shutil.rmtree(agent_context)

    # Ensure .agent-context is gitignored BEFORE copying (critical: prevents
    # untracked files from blocking preflight on the next run)
    _ensure_gitignore_entry(src_dir)

    # task dir → src/.agent-context/task/
    shutil.copytree(task.path, agent_context / "task")

    # global/ → src/.agent-context/global/ (optional)
    global_src = project_root / "global"
    if global_src.exists():
        shutil.copytree(global_src, agent_context / "global")

    # verifications/ → src/.agent-context/verifications/
    verifications_src = project_root / "verifications"
    shutil.copytree(verifications_src, agent_context / "verifications")


def cleanup(project_root: Path) -> None:
    """Remove src/.agent-context/. Called on the success path before committing."""
    agent_context = project_root / "src" / ".agent-context"
    if agent_context.exists():
        shutil.rmtree(agent_context)
