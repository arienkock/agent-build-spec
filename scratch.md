# Agent Build Spec

This document describes a standard aimed at a structured task implementation workflow for coding agents. It is divided into two parts: the **Structure Spec**, which defines the filesystem layout and its rules, and the **Process Spec**, which defines the runtime behavior that operates on top of that structure.

---

# Part 1: Structure Spec

## Filesystem Layout

The root directory of an Agent Build Spec project **MUST** have the following structure

```
.
├── tasks/
│   └── <Task ID>/
│       └── TASK.md
├── global/
│   └── GLOBAL.md
├── verifications/
│   └── <Verification ID>.md
├── results/
│   ├── results-<Task ID>.json
│   └── results-<Task ID>--run-<Order>.json
└── src/
```

- `tasks/.../`: Multiple subdirectories. One per task.
- `TASK.md`: Entrypoint for task instructions.
- `global/GLOBAL.md`: Entrypoint for global instructions valid across all tasks.
- `verifications/<...>.md`: Entrypoints for verification checks.
- `results/`: Directory containing all task results records.
- `src/`: Persistent source directory that serves as the base for agent workspaces.

## Layers and Dependencies

Files marked as "entrypoints" **MAY** refer to other files by relative path as a way to implement progressive disclosure. Agents **MAY** progressively load them as necessary. This is intentional: agents are trusted to decide which referenced files are relevant to their current work, allowing capable agents to manage their own context pressure rather than loading all information upfront.

Task directories **MUST** be self-contained. The directory names **SHOULD** be stable, as they will serve as implicit task IDs. The instructions **MUST NOT** refer to files outside of the task directory. However, instructions **MAY** refer to concepts defined in the global instructions, since those will be made available to the agent. A task **MAY** also use names or terms introduced by previous tasks (e.g. a module name like `AuthService`), since the agent will encounter those artifacts directly in the workspace. Task instructions **MUST NOT** refer to previous tasks by name or ID, as the agent has no visibility into the task structure.

Global instructions defined in `global/GLOBAL.md` **MUST NOT** refer to files in the task directories by name.

Files in the verifications directory **MUST** be self-contained and **MUST NOT** refer to task files by name.

## Concepts

### Task

A Task is a unit of work represented by a subdirectory within `tasks/`. All the task directories represent an ordered backlog of work. The task directory name **IS** the task ID; the order of tasks is defined by the lexicographical ordering of these IDs. Task directory names **SHOULD** begin with an alphanumeric prefix to make ordering explicit and unambiguous (e.g. `001-setup`, `002-auth`, `001b-setup-extra`, `01.1-init`). The task directory **MUST** contain a `TASK.md` file as the entrypoint for task instructions. Additional files **MAY** be included and referenced from `TASK.md` for progressive disclosure. Task instructions **MUST** be written in Markdown.

### Verification

A Verification is a validation check defined by a file in the `verifications/` directory, each named with a unique verification ID (e.g., `001-unit-tests`, `002-lint`, `001b-smoke`). Verification filenames **SHOULD** begin with an alphanumeric prefix to make ordering explicit and unambiguous. Verifications **MAY** define shell commands (e.g. `pytest`, `npm run test`, `./integration-tests.sh`) that are executed with `src/` as the working directory; a verification passes when its command exits with a zero status code. It is **RECOMMENDED** that the first task scaffolds a project structure that allows all verification commands to pass when run from `src/`. It is also **RECOMMENDED** that this scaffolding task creates a `.gitignore` to prevent build artifacts and other unwanted files from being included in automatic commits.

### Global Instructions

Global Instructions are provided in `global/GLOBAL.md` and contain context applicable across all tasks. Task instructions **MAY** reference concepts defined in global instructions, as they are made available to the agent.

---

# Part 2: Process Spec

The Process Spec defines how an automated system executes tasks against the structure defined in Part 1.

## Concepts

### Resume Point

The Resume Point is the task selected for execution before a Task Run begins. The system **MUST** determine an unambiguous resume point by inspecting the task results records in lexicographical task order. The resume point is unambiguous in exactly four cases:

- **No records exist** — begin from the first task.
- **The latest record for the last recorded task is not a success** — re-run that task (this acts as an explicit additional retry). If the status is `running`, the system **MUST** require explicit user confirmation before proceeding, as this may indicate a concurrent or interrupted run; if confirmed, re-run that task from the beginning.
- **All records are successful, and further tasks exist with no records** — the first task without a record is the resume point.
- **All tasks have successful records and no further tasks exist** — the project is complete; there is nothing to run.

Any other state — such as a failed or missing result for an intermediate task followed by a successful result for a later task — is ambiguous. The system **MUST** abort with an error in such cases, requiring manual intervention before proceeding.

As an exception, the user **MAY** explicitly request execution of a specific task out of sequence. The system **MUST** require explicit user confirmation before proceeding. If confirmed, the system **MUST** write a results record with status `skipped` for each intermediate task that has no latest record, following the normal archiving procedure. Intermediate tasks that already have a latest record are left untouched. This makes the state unambiguous before proceeding.

The system **MUST** perform a discrepancy check before resume point logic. If any results record references a task ID that no longer exists on disk, the task order is ambiguous and the system **MUST** abort with an error. The system **MAY** assist the user in reconciling the differences (e.g. by listing the discrepancies and suggesting corrective actions).

### Task Run

A Task Run is the complete execution cycle for a specific task, including preflight checks, workspace preparation, agent invocation, verification, retries, and result recording. A Task Run requires a task ID, which is determined by the Resume Point before the run begins. Task runs **MUST** include timeouts and retry limits for both agent execution and verifications.

### Results Records

A Results Record is a JSON file that documents the outcome of a task run. The latest record for a task **MUST** be named `results-<Task ID>.json`. Before writing a new result, any existing latest record **MUST** be renamed to `results-<Task ID>--run-<Order>.json`, where `<Order>` is `count_of_existing_archived_records + 1`, giving the first archived record `--run-1`. Archived records **MUST** be retained unless explicitly removed.

The latest record **MUST** include a `previousResults` field containing the filename of the immediately preceding archived record, or `null` if no prior run exists. This creates an explicit history chain between records independent of the ordering counter.

The record **MUST** include:
- the base commit ID (the HEAD commit at the start of the task run; preflight ensures this is a clean state unless the user has overridden that check)
- start and end timestamps
- task run status (running, skipped, completed, failed)
- CPU user time, system time, and IO time
- implementation-defined "cost & effort" metrics (e.g. input and output token count, monetary value of API usage)

## Task Run Process

### Resume Point Determination

Before any Task Run begins, the system **MUST** determine the Resume Point as defined above. Only once an unambiguous task ID has been established does the Task Run Process proceed.

### Preflight Checks

Before initiating a task run, the system **MUST** verify that the project root is a Git repository. It **SHOULD** check for uncommitted changes in `tasks/`, `verifications/`, and `src/` directories, and **MUST** require explicit user confirmation before proceeding if any are found.

### Workspace Preparation

The agent operates in-place within `src/`. The system **MUST** copy context files into `src/.agent-context/` using the following canonical layout before invoking the agent:

```
.agent-context/
├── task/          # contents of the current task directory
├── global/        # contents of global/
└── verifications/ # contents of verifications/
```

The system **MUST** ensure that `src/.agent-context/` does not already exist before copying; if it does, the task run **MUST** abort with an error. The error **SHOULD** indicate that a previous run likely left the directory behind and that it must be removed before proceeding. The system **MAY** offer to remove it on the user's behalf.

### Agent Invocation

The agent is invoked as a non-interactive subprocess with captured STDIO. The agent operates within the workspace and **MUST NOT** alter the parent terminal state.

### Verification Execution

Upon agent completion, verifications are executed in lexicographical order of their ID. The first failing verification halts further verification. Its output **MAY** be summarized, and **MUST** be appended to the original task prompt (before any retries) before re-invoking the agent as a retry.

### Retries and Timeouts

Task runs **MUST** support a configurable timeout for agent execution. If the agent exceeds this timeout, the system **MUST** retry it with the same prompt that was used for the timed-out invocation, up to a configurable retry limit. The retry prompt is intentionally identical to the original: capable agents are expected to inspect the workspace, identify what has already been done, and continue from there without explicit instruction. If the agent terminates with an error, no retry is attempted and the task run **MUST** be recorded as failed. Verification failures trigger a retry as described above, also subject to a configurable retry limit.

### Result Recording and Commit

Before committing, the system **MUST** remove `src/.agent-context/`. Successful task runs **MUST** then append a results record and create a commit.

---

# Appendix: Example Prompts

The following examples illustrate how a compliant system might construct prompts for agent invocation. The exact wording and format are left to the implementing system.

## Example: Initial Task Prompt

```
You are a coding agent.

Your task instructions are in `.agent-context/task/TASK.md`. Read them before doing
anything else. The file may reference additional files within `.agent-context/task/`
for progressive disclosure — load them as needed.

Global instructions that apply to all tasks are in `.agent-context/global/GLOBAL.md`.
Read these before beginning work.

Verification checks that will be run against your output are defined as files in
`.agent-context/verifications/`. You MAY review them in advance to understand what
success looks like.

Complete the task. When you are done, stop. Verifications will be run automatically.
```

## Example: Verification Retry Prompt

```
[The original task prompt, reproduced in full]

---

The following verification failed. Review the output and correct the issue.

Verification: `.agent-context/verifications/001-unit-tests.md`
Exit code: 1

Output:
FAILED tests/test_auth.py::test_login_returns_token - AssertionError: expected 200, got 401
1 failed, 14 passed in 3.21s
```
