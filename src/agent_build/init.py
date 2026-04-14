from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

CONFIG_FILENAME = "agent-build.config.json"

GITIGNORE_DEFAULTS = [
    ".env",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".venv/",
    "venv/",
    "node_modules/",
    ".DS_Store",
    "*.swp",
    "*.swo",
]

CONFIG_DEFAULTS = {
    "agent_command": "claude --print --dangerously-skip-permissions --model {model}",
    "model": "claude-sonnet-4-6",
    "agent_timeout_seconds": 600,
    "verification_timeout_seconds": 120,
    "max_retries": 3,
}

GLOBAL_TEMPLATE = """\
# Global Instructions

Add instructions here that apply to all tasks. These will be provided to the
agent before each task's specific instructions.
"""

REQUIRED_PATHS = ["tasks", "src", "verifications", "global", CONFIG_FILENAME]


class InitError(Exception):
    pass


def _get_package_templates_dir() -> Path:
    return Path(__file__).parent / "templates"


def find_template(name_or_path: str, project_root: Path | None = None) -> Path:
    if Path(name_or_path).is_absolute():
        path = Path(name_or_path)
        if not path.is_dir():
            raise InitError(f"Template path does not exist: {name_or_path}")
        return path

    pkg_dir = _get_package_templates_dir()
    builtin = pkg_dir / name_or_path
    if builtin.is_dir():
        return builtin

    if project_root is not None:
        local = project_root / ".agent-build" / "templates" / name_or_path
        if local.is_dir():
            return local

    path = Path(name_or_path)
    if path.is_dir():
        return path

    available = [d.name for d in pkg_dir.iterdir() if d.is_dir()]
    raise InitError(
        f"Template '{name_or_path}' not found. "
        f"Available built-in templates: {', '.join(available) if available else 'none'}"
    )


def list_builtin_templates() -> list[str]:
    pkg_dir = _get_package_templates_dir()
    return sorted([d.name for d in pkg_dir.iterdir() if d.is_dir()])


def _copy_template_files(
    template_dir: Path,
    project_root: Path,
    force: bool = False,
) -> None:
    subdirs = ["tasks", "verifications", "global", "src"]
    for subdir in subdirs:
        src_path = template_dir / subdir
        if not src_path.is_dir():
            continue
        dest_path = project_root / subdir
        if dest_path.exists():
            if force:
                shutil.rmtree(dest_path)
                shutil.copytree(src_path, dest_path)
            else:
                for item in src_path.rglob("*"):
                    if item.is_file():
                        rel = item.relative_to(src_path)
                        dest_file = dest_path / rel
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest_file)
        else:
            shutil.copytree(src_path, dest_path)

    src_config = template_dir / CONFIG_FILENAME
    if src_config.is_file():
        dest_config = project_root / CONFIG_FILENAME
        if not dest_config.exists() or force:
            shutil.copy2(src_config, dest_config)


def _create_structure(
    project_root: Path,
    force: bool = False,
    git: bool = False,
    global_flag: bool = False,
) -> None:
    if git:
        subprocess.run(
            ["git", "init"], cwd=project_root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "agent-build@example.com"],
            cwd=project_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "agent-build"],
            cwd=project_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=project_root,
            check=True,
            capture_output=True,
        )

    for subdir in ["tasks", "verifications", "global", "src"]:
        dest = project_root / subdir
        if not dest.exists():
            dest.mkdir(parents=True)

    src_gitignore = project_root / "src" / ".gitignore"
    _write_gitignore(src_gitignore)

    if global_flag:
        (project_root / "global" / "GLOBAL.md").write_text(GLOBAL_TEMPLATE)

    config_path = project_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(json.dumps(CONFIG_DEFAULTS, indent=2) + "\n")


def _write_gitignore(path: Path) -> None:
    if path.exists():
        existing = path.read_text()
        lines = existing.rstrip("\n").split("\n")
        existing_entries = {line.rstrip() for line in lines if line.strip()}
    else:
        existing_entries = set()

    new_entries = [e for e in GITIGNORE_DEFAULTS if e not in existing_entries]
    if new_entries:
        with path.open("a") as f:
            if existing_entries:
                f.write("\n")
            for entry in new_entries:
                f.write(entry + "\n")


def init_project(
    project_root: Path,
    force: bool = False,
    git: bool = False,
    global_flag: bool = False,
    template: str | None = None,
    yes: bool = False,
) -> None:
    if not project_root.is_dir():
        raise InitError(f"Directory does not exist: {project_root}")

    if force:
        if not yes:
            import click

            if not click.confirm(
                "This will remove existing files and directories. Continue?"
            ):
                raise InitError("Aborted.")
        _remove_existing(project_root)
        force_create = True
    else:
        existing = [p for p in REQUIRED_PATHS if (project_root / p).exists()]
        if existing:
            listed = ", ".join(sorted(existing))
            raise InitError(
                f"Project already initialized. Found: {listed}. "
                "Use --force to reinitialize."
            )
        force_create = False

    if template:
        template_path = find_template(template, project_root)
        _copy_template_files(template_path, project_root, force=force_create)

    _create_structure(
        project_root, force=force_create, git=git, global_flag=global_flag
    )


def _remove_existing(project_root: Path) -> None:
    for name in ["tasks", "verifications", "global", "src", "results", CONFIG_FILENAME]:
        path = project_root / name
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
