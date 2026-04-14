from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_build.init import (
    CONFIG_DEFAULTS,
    GITIGNORE_DEFAULTS,
    InitError,
    find_template,
    init_project,
    list_builtin_templates,
)


class TestFindTemplate:
    def test_finds_builtin_minimal(self):
        template = find_template("minimal")
        assert template.is_dir()
        assert template.name == "minimal"

    def test_finds_builtin_python(self):
        template = find_template("python")
        assert template.is_dir()
        assert template.name == "python"

    def test_loads_local_path(self, tmp_path):
        template_dir = tmp_path / "my-template"
        template_dir.mkdir()
        (template_dir / "tasks").mkdir()
        template = find_template(str(template_dir))
        assert template == template_dir

    def test_invalid_template_raises_error(self):
        with pytest.raises(InitError) as exc_info:
            find_template("nonexistent")
        assert "not found" in str(exc_info.value)
        assert "nonexistent" in str(exc_info.value)


class TestListBuiltinTemplates:
    def test_returns_list(self):
        templates = list_builtin_templates()
        assert isinstance(templates, list)
        assert "minimal" in templates
        assert "python" in templates


class TestInitProject:
    def test_creates_expected_structure(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project)

        assert (project / "tasks").is_dir()
        assert (project / "verifications").is_dir()
        assert (project / "global").is_dir()
        assert (project / "src").is_dir()
        assert (project / "src" / ".gitignore").is_file()
        assert (project / "agent-build.config.json").is_file()

    def test_refuses_if_dirs_exist(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "tasks").mkdir()

        with pytest.raises(InitError) as exc_info:
            init_project(project)
        assert "already initialized" in str(exc_info.value)

    def test_force_removes_and_recreates(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "tasks").mkdir()
        (project / "tasks" / "old-task").mkdir()

        init_project(project, force=True, yes=True)

        assert (project / "tasks").is_dir()
        assert not (project / "tasks" / "old-task").exists()

    def test_force_requires_yes_confirmation(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        (project / "tasks").mkdir()

        import click

        confirmed = False

        def fake_confirm(msg):
            nonlocal confirmed
            confirmed = True
            return False

        original_confirm = click.confirm
        monkeypatch.setattr(click, "confirm", fake_confirm)

        try:
            with pytest.raises(InitError) as exc_info:
                init_project(project, force=True, yes=False)
            assert "Aborted" in str(exc_info.value)
            assert confirmed
        finally:
            click.confirm = original_confirm

    def test_git_initializes_repo(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project, git=True)

        assert (project / ".git").is_dir()
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=project,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_no_git_by_default_if_git_exists(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)

        init_project(project, git=None)

        assert (project / ".git").is_dir()

    def test_global_creates_global_md(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project, global_flag=True)

        global_md = project / "global" / "GLOBAL.md"
        assert global_md.is_file()
        assert "Global Instructions" in global_md.read_text()

    def test_template_minimal_no_extra_files(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project, template=None)

        assert (project / "tasks").is_dir()
        assert not list((project / "tasks").iterdir())

    def test_template_python_copies_tasks(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project, template="python")

        assert (project / "tasks" / "001-setup" / "TASK.md").is_file()
        assert (project / "tasks" / "002-impl" / "TASK.md").is_file()

    def test_template_force_replaces_existing_content(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "tasks").mkdir()
        (project / "tasks" / "001-existing").mkdir()

        init_project(project, template="python", force=True, yes=True)

        assert not (project / "tasks" / "001-existing").exists()
        assert (project / "tasks" / "001-setup").exists()
        assert (project / "tasks" / "002-impl").exists()

    def test_template_force_overwrites(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "tasks").mkdir()
        (project / "tasks" / "001-setup").mkdir()

        init_project(project, template="python", force=True, yes=True)

        assert (project / "tasks" / "001-setup" / "TASK.md").is_file()

    def test_gitignore_contains_expected_entries(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project)

        gitignore = project / "src" / ".gitignore"
        assert gitignore.is_file()
        content = gitignore.read_text()
        for entry in GITIGNORE_DEFAULTS:
            assert entry in content

    def test_gitignore_created_if_absent(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project)

        gitignore = project / "src" / ".gitignore"
        assert gitignore.is_file()
        for entry in GITIGNORE_DEFAULTS:
            assert entry in gitignore.read_text()

    def test_config_created_with_defaults(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        init_project(project)

        config = project / "agent-build.config.json"
        data = json.loads(config.read_text())
        for key, value in CONFIG_DEFAULTS.items():
            assert key in data
            assert data[key] == value

    def test_exit_code_zero_on_success(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        result = subprocess.run(
            ["agent-build", "init"],
            cwd=project,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_exit_code_one_on_error(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "tasks").mkdir()

        result = subprocess.run(
            ["agent-build", "init"],
            cwd=project,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "already initialized" in result.stderr

    def test_local_template_path(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        local_template = tmp_path / "local-template"
        local_template.mkdir()
        (local_template / "tasks" / "local-task").mkdir(parents=True)
        (local_template / "tasks" / "local-task" / "TASK.md").write_text("# Local")

        result = subprocess.run(
            ["agent-build", "init", "--template", str(local_template)],
            cwd=project,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert (project / "tasks" / "local-task" / "TASK.md").is_file()

    def test_invalid_template_error(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        result = subprocess.run(
            ["agent-build", "init", "--template", "does-not-exist"],
            cwd=project,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr
