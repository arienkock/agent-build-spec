# Agent Build Spec

This document describes a standard aimed at a structured task implementation workflow for coding agents. It is divided into two parts: the **Structure Spec**, which defines the filesystem layout and its rules, and the **Process Spec**, which defines the runtime behavior that operates on top of that structure.

---

# Part 1: Structure Spec

## Filesystem Layout

The root directory of an Agent Build Spec project **MUST** have the following structure

```
.
├── tasks/
│   ├── <Task ID>/
│   │   └── TASK.md
│   └── GLOBAL.md
├── verifications/
│   └── <Verification ID>.md
└── src/
```

- `tasks/.../`: Multiple subdirectories. One per task.
- `TASK.md`: Entrypoint for task instructions.
- `GLOBAL.md`: Entrypoint for global instructions valid across all tasks.
- `verifications/<...>.md`: Entrypoints for verification checks.
- `src/`: Self-contained workspace for agents.

## Layers and Dependencies

Files marked as "entrypoints" **MAY** refer to other files by relative path as a way to implement progressive disclosure. Agents **MAY** progressively load them as necessary.

Task directories **MUST** be self-contained. The directory names **SHOULD** be stable, as they will serve as implicit task IDs. The instructions **MUST NOT** refer to files outside of the task directory. However, instructions **MAY** refer to concepts defined in the global instructions, since those will be made available to the agent. A task **MAY** also refer to concepts implemented in previous tasks, so long as they appear with consistent naming in the workspace files.

Global instructions defined in `GLOBAL.md` **MUST NOT** refer to files in the task directories by name.

Files in the verifications directory **MUST** be self-contained and **MUST NOT** refer to task files by name.

## Concepts

### Task

A Task is a unit of work represented by a subdirectory within `tasks/`. All the task directories represent an ordered backlog of work. The order of tasks is defined by the lexicographical ordering of the task IDs. The task directory **MUST** contain a `TASK.md` file as the entrypoint for task instructions. Additional files **MAY** be included and referenced from `TASK.md` for progressive disclosure. Task instructions **MUST** be written in unstructured Markdown.

### Verification

A Verification is a validation check defined by a file in the `verifications/` directory, each named with a unique verification ID (e.g., `<Verification ID>.md`).

### Global Instructions

Global Instructions are provided in `GLOBAL.md` and contain context applicable across all tasks. Task instructions **MAY** reference concepts defined in global instructions, as they are made available to the agent. Tasks **MAY** also reference concepts from previous tasks if consistently named in the workspace.

---

# Part 2: Process Spec

The Process Spec defines how an automated system executes tasks against the structure defined in Part 1.

## Concepts

### Task Run

A Task Run is the complete execution cycle for a task, including preflight checks, workspace setup, agent invocation, verification, retries, and result recording. Task runs **MUST** determine an unambiguous resume point. They **MUST** include timeouts and retry limits for both agent execution and verifications.

### Results Records

A Results Record is a JSON file that documents the outcome of a task run. Each task **MUST** have one results record file named after its task ID. Previous task run results **MUST** be retained, unless explicitly removed. Previous task run records **MUST** use distinct filenames to differentiate them from the latest record.

The record **MUST** include:
- the base commit ID
- start and end timestamps
- task run status (running, skipped, completed, failed)
- "cost & effort" metrics as returned by the agent

Example cost and effort metrics:
- CPU user, system, and IO time
- input and output token count
- monetary value of API usage

## Task Run Process

### Preflight Checks

Before initiating a task run, the system **MUST** verify that the project root is a Git repository. It **SHOULD** check for uncommitted changes in `tasks/`, `verifications/`, and `src/` directories, unless overridden by a `--force` flag.

### Workspace Creation

A workspace **MAY** be created as a copy of the `src/` directory. If a workspace is created, it **MUST** be initialized as a Git repository.

### Agent Invocation

The agent is invoked as a non-interactive subprocess with captured STDIO. The agent operates within the workspace and **MUST NOT** alter the parent terminal state. Agents **MAY** optionally return token cost metrics.

### Verification Execution

Upon agent completion, verifications are executed in lexicographical order of their ID. Verification runs **MAY** reuse the existing agent session or create a fresh session per verification (default is reuse). The first failing verification **MUST** be processed and fed back into a retry session.

### Retries and Timeouts

Task runs **MUST** support retries for failed agent executions and failed verifications, each with configurable limits and timeouts.

### Result Recording and Commit

Successful task runs **MUST** merge changes from the workspace back to `src/`, append a results record, and create a commit.
