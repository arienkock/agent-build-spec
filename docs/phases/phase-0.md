# Phase 0 — Project Initialization

**CLI:** `agent-build init` (creates a new build spec project).

## `agent-build init` Command

Creates a complete, spec-compliant project structure. Optionally initializes a git repository if none exists.

### Directory structure created

```
<project-root>/
├── tasks/              # empty; ready for task subdirectories
├── verifications/      # empty; ready for verification files
├── global/             # empty; optional GLOBAL.md created if --global flag
├── src/                # empty; agent workspace
│   └── .gitignore      # created with sensible defaults (see below)
├── results/            # empty; created on first task run
└── agent-build.config.json  # default configuration
```

### `.gitignore` defaults (appended to existing or created)

```
.env
__pycache__/
*.pyc
.pytest_cache/
.venv/
venv/
node_modules/
.DS_Store
*.swp
*.swo
```

### `agent-build.config.json` defaults

```json
{
  "agent_command": "claude --print --dangerously-skip-permissions --model {model}",
  "model": "claude-sonnet-4-6",
  "agent_timeout_seconds": 600,
  "verification_timeout_seconds": 120,
  "max_retries": 3
}
```

### Behavior

- Refuses to run if any of the following already exist: `tasks/`, `src/`, `verifications/`, `global/`, `agent-build.config.json`
- With `--force`: removes and recreates (destructive; requires `--yes` confirmation)
- With `--git` (default if no git repo exists): runs `git init` before creating structure
- With `--global`: creates `global/GLOBAL.md` with template header:
  ```markdown
  # Global Instructions

  Add instructions here that apply to all tasks. These will be provided to the
  agent before each task's specific instructions.
  ```
- `--template <name>`: loads a template from a built-in registry or local path to seed initial tasks/verifications. Default templates:
  - `minimal` (default): empty structure
  - `python`: includes `001-setup/`, `002-impl/` tasks with sensible defaults for Python projects
- `agent_build.config.json` is always created with full defaults; the `{model}` placeholder is validated at config load time, not init time.

### Extensibility

- Templates are discovered from: (1) built-in registry, (2) `<project-root>/.agent-build/templates/<name>/`, (3) paths passed via `--template`
- A template is a directory containing any of: `tasks/`, `verifications/`, `global/`, `src/`, `agent-build.config.json`
- Files from the template are copied into the project root; existing files are skipped unless `--force` is used
- Users may extend the tool by placing custom templates in `.agent-build/templates/` within any project root

### Exit codes

0 on success, 1 on error (existing files without `--force`, git init failure, invalid template, etc.).

## Testing

| Module | Key cases |
|---|---|
| `init.py` / `cli.py` (init) | creates expected structure; refuses if dirs exist (no --force); --force removes and recreates; --force requires --yes; --git initializes repo; --global creates GLOBAL.md; --template loads built-in template; --template with local path copies files; template files skip existing unless --force; invalid template → error; empty project root → success; .gitignore appended if exists, created if absent; .gitignore contains expected entries; agent-build.config.json created with all defaults; exit code 0 on success, 1 on error |
