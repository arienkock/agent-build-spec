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

A Task is a unit of work represented by a subdirectory within `tasks/`. All the task directories represent an ordered backlog of work. The task directory name **IS** the task ID; the order of tasks is defined by the lexicographical ordering of these IDs. Task directory names **SHOULD** begin with an alphanumeric prefix to make ordering explicit and unambiguous (e.g. `001-setup`, `002-auth`, `001b-setup-extra`, `01.1-init`). The task directory **MUST** contain a `TASK.md` file as the entrypoint for task instructions. Additional files **MAY** be included and referenced from `TASK.md` for progressive disclosure. Task instructions **MUST** be written in unstructured Markdown.

### Verification

A Verification is a validation check defined by a file in the `verifications/` directory, each named with a unique verification ID (e.g., `<Verification ID>.md`). Verifications **MAY** define shell commands (e.g. `pytest`, `npm run test`, `./integration-tests.sh`) that are executed with `src/` as the working directory; a verification passes when its command exits with a zero status code. It is **RECOMMENDED** that the first task scaffolds a project structure that allows all verification commands to pass when run from `src/`. It is also **RECOMMENDED** that this scaffolding task creates a `.gitignore` to prevent build artifacts and other unwanted files from being included in automatic commits.

### Global Instructions

Global Instructions are provided in `global/GLOBAL.md` and contain context applicable across all tasks. Task instructions **MAY** reference concepts defined in global instructions, as they are made available to the agent.

---

# Part 2: Process Spec

The Process Spec defines how an automated system executes tasks against the structure defined in Part 1.

## Concepts

### Resume Point

The Resume Point is the task selected for execution before a Task Run begins. The system **MUST** determine an unambiguous resume point by inspecting the task results records in lexicographical task order. The resume point is unambiguous in exactly three cases:

- **No results exist** — begin from the first task.
- **Last result was not a success** — re-run that task (this acts as an explicit additional retry).
- **All tasks have a successful result** — the next task in lexicographical order is the resume point.

Any other state — such as a failed or missing result for an intermediate task followed by a successful result for a later task — is ambiguous. The system **MUST** abort with an error in such cases, requiring manual intervention before proceeding.

As an exception, the user **MAY** explicitly request execution of a specific task out of sequence. The system **MUST** require explicit user confirmation before proceeding. If confirmed, the system **MUST** create placeholder results records with status `skipped` for all intermediate tasks that lack a result, making the state unambiguous before proceeding.

The system **MUST** also compare the set of task directories on disk against the set of task IDs referenced in results records. If there is any discrepancy — a task exists on disk with no corresponding result, or a result references a task ID no longer present on disk — the system **MUST** require explicit user confirmation before proceeding. The system **MAY** assist the user in reconciling the differences (e.g. by listing the discrepancies and suggesting corrective actions).

### Task Run

A Task Run is the complete execution cycle for a specific task, including preflight checks, workspace preparation, agent invocation, verification, retries, and result recording. A Task Run requires a task ID, which is determined by the Resume Point before the run begins. Task runs **MUST** include timeouts and retry limits for both agent execution and verifications.

### Results Records

A Results Record is a JSON file that documents the outcome of a task run. The latest record for a task **MUST** be named `results-<Task ID>.json`. Before writing a new result, any existing latest record **MUST** be renamed to `results-<Task ID>--run-<Order>.json`, where `<Order>` is an incrementing integer derived from the count of existing archived records for that task. Archived records **MUST** be retained unless explicitly removed.

The latest record **MUST** include a `previousResults` field containing the filename of the immediately preceding archived record, or `null` if no prior run exists. This creates an explicit history chain between records independent of the ordering counter.

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

### Resume Point Determination

Before any Task Run begins, the system **MUST** determine the Resume Point as defined above. Only once an unambiguous task ID has been established does the Task Run Process proceed.

### Preflight Checks

Before initiating a task run, the system **MUST** verify that the project root is a Git repository. It **SHOULD** check for uncommitted changes in `tasks/`, `verifications/`, and `src/` directories, and **MUST** require explicit user confirmation before proceeding if any are found.

### Workspace Preparation

The agent operates in-place within `src/`. The system **MUST** copy the current task's directory, the global instructions, and all verification files into `src/` so the agent has access to them. The system **MUST** ensure that none of the copied paths conflict with paths already present in `src/`; if a conflict is detected, the task run **MUST** abort with an error.

### Agent Invocation

The agent is invoked as a non-interactive subprocess with captured STDIO. The agent operates within the workspace and **MUST NOT** alter the parent terminal state. Agents **MAY** optionally return token cost metrics.

### Verification Execution

Upon agent completion, verifications are executed in lexicographical order of their ID. The first failing verification halts further verification. Its output **MAY** be summarized, and **MUST** be appended to the original agent prompt before re-invoking the agent as a retry.

### Retries and Timeouts

Task runs **MUST** support a configurable timeout for agent execution. If the agent exceeds this timeout, the system **MUST** retry it with the same original prompt, up to a configurable retry limit. If the agent terminates with an error, no retry is attempted and the task run **MUST** be recorded as failed. Verification failures trigger a retry as described above, also subject to a configurable retry limit.

### Result Recording and Commit

Before committing, the system **MUST** remove the copied task, global instructions, and verification files from `src/`. Successful task runs **MUST** then append a results record and create a commit.
