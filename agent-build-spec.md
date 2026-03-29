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
├── global/           # optional
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
- `global/GLOBAL.md`: Entrypoint for global instructions valid across all tasks. This directory and file are **optional**; if absent, no global instructions are provided to the agent.
- `verifications/<...>.md`: Entrypoints for verification checks.
- `results/`: Directory containing all task results records. Created automatically on first use.
- `src/`: Persistent source directory that serves as the base for agent workspaces.

## Layers and Dependencies

Files marked as "entrypoints" **MAY** refer to other files by relative path as a way to implement progressive disclosure. Agents **MAY** progressively load them as necessary. This is intentional: agents are trusted to decide which referenced files are relevant to their current work, allowing capable agents to manage their own context pressure rather than loading all information upfront.

Task directories **MUST** be self-contained. The directory names **SHOULD** be stable, as they will serve as implicit task IDs. The instructions **MUST NOT** refer to files outside of the task directory. However, instructions **MAY** refer to concepts defined in the global instructions, since those will be made available to the agent. A task **MAY** also use names or terms introduced by previous tasks (e.g. a module name like `AuthService`), since the agent will encounter those artifacts directly in the workspace. Task instructions **MUST NOT** refer to previous tasks by name or ID, as the agent has no visibility into the task structure.

Global instructions defined in `global/GLOBAL.md` **MUST NOT** refer to files in the task directories by name.

Files in the verifications directory **MUST** be self-contained and **MUST NOT** refer to task files by name.

## Concepts

### Task

A Task is a unit of work represented by a subdirectory within `tasks/`. All the task directories represent an ordered backlog of work. The task directory name **IS** the task ID; the order of tasks is defined by the lexicographical ordering of these IDs. Task directory names **SHOULD** begin with an alphanumeric prefix to make ordering explicit and unambiguous (e.g. `001-setup`, `002-auth`, `001b-setup-extra`, `01.1-init`). The task directory **MUST** contain a `TASK.md` file as the entrypoint for task instructions; if a task directory exists without a `TASK.md`, the system **MUST** abort with an error. Additional files **MAY** be included and referenced from `TASK.md` for progressive disclosure. Task instructions **MUST** be written in Markdown.

### Verification

A Verification is a validation check defined by a Markdown file in the `verifications/` directory, each named with a unique verification ID (e.g., `001-unit-tests`, `002-lint`, `001b-smoke`). Verification filenames **SHOULD** begin with an alphanumeric prefix to make ordering explicit and unambiguous. Each verification is evaluated by a dedicated verification agent: the system passes the verification file to the agent (with a structured response instruction appended), the agent performs the described checks — which may include running shell commands in the workspace — and responds with `{ "status": "PASS" | "FAIL", "reasoning": "..." }`. A verification passes when the agent returns `"status": "PASS"`. It is **RECOMMENDED** that the first task scaffolds a project structure that allows all verifications to pass from the outset. It is also **RECOMMENDED** that this scaffolding task creates a `.gitignore` to prevent build artifacts and other unwanted files from being included in automatic commits.

### Global Instructions

Global Instructions are optionally provided in `global/GLOBAL.md` and contain context applicable across all tasks. If the `global/` directory or `GLOBAL.md` file is absent, the system **MUST** proceed without global instructions. Task instructions **MAY** reference concepts defined in global instructions, as they are made available to the agent when present.

---

# Part 2: Process Spec

The Process Spec defines how an automated system executes tasks against the structure defined in Part 1.

## Concepts

### Resume Point

The Resume Point is the task selected for execution before a Task Run begins. The system **MUST** determine an unambiguous resume point by inspecting the task results records in lexicographical task order. The resume point is unambiguous in exactly four cases:

- **No records exist** — begin from the first task.
- **The latest record for the last recorded task is not a success** — re-run that task (this acts as an explicit additional retry). If the status is `running`, the system **MUST** require explicit user confirmation before proceeding, as this may indicate a concurrent or interrupted run; if confirmed, re-run that task from the beginning. If more than one task has a latest record with status `running`, the state is ambiguous and the system **MUST** abort with an error.
- **All records are successful, and further tasks exist with no records** — the first task without a record is the resume point.
- **All tasks have successful records and no further tasks exist** — the project is complete; there is nothing to run.

For the purpose of resume point logic, `skipped` is treated as equivalent to `completed`.

Any other state — such as a failed or missing result for an intermediate task followed by a successful result for a later task — is ambiguous. The system **MUST** abort with an error in such cases, requiring manual intervention before proceeding.

As an exception, the user **MAY** explicitly request execution of a specific task by ID. This includes running a task that already has a successful or skipped record (e.g., to re-run an earlier task). The system **MUST** require explicit user confirmation before proceeding. If confirmed, the system **MUST** write a results record with status `skipped` for each intermediate task (between the current resume point and the target) that has no latest record, following the normal archiving procedure. Intermediate tasks that already have a latest record are left untouched. This makes the state unambiguous before proceeding.

The system **MUST** perform a discrepancy check before resume point logic. If any results record references a task ID that no longer exists on disk, the task order is ambiguous and the system **MUST** abort with an error. The system **MAY** assist the user in reconciling the differences (e.g. by listing the discrepancies and suggesting corrective actions).

### Task Run

A Task Run is the complete execution cycle for a specific task, including preflight checks, workspace preparation, agent invocation, verification, retries, and result recording. A Task Run requires a task ID, which is determined by the Resume Point before the run begins. Task runs **MUST** include timeouts and retry limits for both agent execution and verifications.

### Results Records

A Results Record is a JSON file that documents the outcome of a task run. The `results/` directory **MUST** be created automatically if it does not exist. The latest record for a task **MUST** be named `results-<Task ID>.json`. Before writing a new result, any existing latest record **MUST** be renamed to `results-<Task ID>--run-<Order>.json`, where `<Order>` is `max_existing_archived_order + 1` (or `1` if no archived records exist). Using the maximum rather than the count ensures correctness even if archived records have been manually removed. Archived records **MUST** be retained unless explicitly removed. Implementations **SHOULD** use atomic filesystem operations (e.g., write to a temporary file then rename) when creating or replacing the latest record, to minimise the window in which the record is absent or partially written.

The latest record **MUST** include a `previousResults` field containing the filename of the immediately preceding archived record, or `null` if no prior run exists. This creates an explicit history chain between records independent of the ordering counter.

Records with status `skipped` are a special case: they **MUST** contain only the `status` field (set to `skipped`) and the `previousResults` field. All other fields defined below are **NOT** applicable and **MUST NOT** be included.

For all other statuses, the record **MUST** include:
- the base commit ID (the HEAD commit at the start of the task run; preflight ensures this is a clean state unless the user has overridden that check)
- start and end timestamps
- task run status (running, completed, failed)
- CPU user time, system time, and IO time
- implementation-defined "cost & effort" metrics (e.g. input and output token count, monetary value of API usage)

Result records are written and kept up to date throughout the task run via event subscriptions. Both the task runner and the agent runner dispatch events (e.g., run started, agent progress, run completed), and the record writer **SHOULD** subscribe to these rather than writing the record only at the end. This means a record with status `running` **MUST** be written before agent invocation begins. Fields that are only known after the run — such as end timestamp, final status, and accumulated metrics — **MUST** be updated as the corresponding events are received. This approach is consistent with the requirement that the task runner listen for asynchronous messages from the agent runner (see Agent Invocation), and it ensures that a crash or unexpected termination leaves a recoverable `running` record rather than no record at all.

## Task Run Process

### Resume Point Determination

Before any Task Run begins, the system **MUST** determine the Resume Point as defined above. Only once an unambiguous task ID has been established does the Task Run Process proceed.

### Preflight Checks

Before initiating a task run, the system **MUST** verify that the project root is a Git repository. It **MUST** also verify that the required directories `tasks/` and `verifications/` exist; if either is absent, the system **MUST** abort with an error. If `tasks/` contains no task subdirectories, the system **MUST** abort with an error. The system **MUST** check for any uncommitted or untracked changes anywhere in the repository, and **MUST** require explicit user confirmation before proceeding if any are found.

To guard against concurrent runs, the system **SHOULD** acquire a lock by creating a lock file (e.g., `.agent-build.lock`) in the project root at the start of preflight and releasing it when the task run completes or aborts. If the lock file already exists, the system **SHOULD** abort with an error indicating that another run may be in progress. If the lock file's recorded process ID is no longer active, the system **SHOULD** treat it as stale, remove it, and proceed after informing the user.

### Workspace Preparation

The agent operates in-place within `src/`. The system **MUST** copy context files into `src/.agent-context/` using the following canonical layout before invoking the agent:

```
.agent-context/
├── task/          # contents of the current task directory
├── global/        # contents of global/ (omitted if global/ is absent)
└── verifications/ # contents of verifications/
```

The system **MUST** ensure that `src/.agent-context/` does not already exist before copying; if it does, the system **MUST** require explicit user confirmation before proceeding. The prompt **SHOULD** indicate that a previous run likely left the directory behind. If confirmed, the system **MUST** delete it before proceeding with the copy.

### Agent Invocation

The agent is invoked as a non-interactive subprocess with captured STDIO. The agent operates within the workspace and **MUST NOT** alter the parent terminal state. The task runner **MUST** listen for asynchronous messages dispatched by the agent runner while it is executing, so that progress and status information can be acted upon without waiting for the subprocess to terminate.

The system **SHOULD** provide live progress feedback to the user while the agent is running. If the agent implementation exposes token consumption or cost metrics via asynchronous messages, those **SHOULD** be surfaced in real time. Otherwise, the system **SHOULD** periodically report the net lines added and removed across all changes in `src/` relative to the base commit (including untracked, non-ignored files), as a lightweight signal that work is progressing.

### Verification Execution

Upon agent completion, verifications are executed in lexicographical order of their ID. The first verification that returns `"status": "FAIL"` halts further verification. The verification agent's `reasoning` **MUST** be appended to the original task prompt before re-invoking the implementation agent as a retry.

### Retries and Timeouts

Preflight checks and workspace preparation occur once per task run, before the first agent invocation. Retries reuse the already-prepared workspace without repeating these steps.

As noted in the Task Run concept, task runs **MUST** enforce configurable timeouts for both agent execution and verification execution. If the agent exceeds its timeout, the system **MUST** retry it with the same prompt that was used for the timed-out invocation. The retry prompt is intentionally identical to the original: capable agents are expected to inspect the workspace, identify what has already been done, and continue from there without explicit instruction. If the agent exits with a non-zero exit code, it is considered to have terminated with an error: no retry is attempted and the task run **MUST** be recorded as failed. Verification failures trigger a retry as described above; on each retry, only the reasoning from the most recently failed verification is appended to the original prompt — earlier failure outputs are not accumulated. All retries — whether caused by timeouts or verification failures — draw from a single shared configurable retry limit.

On timeout or failure, the contents of `src/` **MUST** be left as-is. The workspace is not reset between automatic retries, nor after the retry limit is exhausted or an error terminates the run. This serves two purposes: it allows the user to inspect the partial state, and it allows the user to trigger an explicit additional run that picks up where the agent left off.

### Result Recording and Commit

Before committing, the system **MUST** remove `src/.agent-context/`. Successful task runs **MUST** then append a results record and create a commit. If there are no changes in either `src/` or `results/` to stage, the system **MUST** abort with an error. The commit **MUST** stage only files within `src/` and `results/`; changes to other directories (e.g. `tasks/`, `verifications/`, `global/`) **MUST NOT** be included.

### Rollback

The system **MUST** support rollback of the latest task run. Rollback **MUST NOT** be applied when the latest results record has status `skipped`; such records carry no base commit and represent no workspace changes, so there is nothing to restore. A failed task run will not normally have a commit, since auto-commit only occurs on success; however, if the user manually committed the workspace after a failed run, the rollback procedure handles this correctly — `src/` is restored to the base commit state and a new commit is created. Before performing a rollback, the system **MUST** verify that the repository has no uncommitted changes and no untracked files; if any are found, the rollback **MUST** abort with an error. If the base commit recorded in the results record is not found in the repository's history, the rollback **MUST** abort with an error. When a rollback is performed:

1. The contents of `src/` **MUST** be restored to the state at the base commit recorded in the latest results record. Only `src/` is restored; other files that may have been committed after the base commit (e.g. in `tasks/`, `verifications/`, or `results/`) are left untouched.
2. The latest results record (`results-<Task ID>.json`) **MUST** be deleted.
3. If a previous archived results record exists (as referenced by the `previousResults` field of the deleted record), it **MUST** be renamed back to `results-<Task ID>.json`, restoring it as the latest record.
4. The resulting changes **MUST** be recorded as a new commit. Git history is append-only; rollback never rewrites or removes existing commits.

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

The following verification failed. Review the reasoning and correct the issue.

Verification: `.agent-context/verifications/001-unit-tests.md`

Reasoning:
Running `pytest` produced one failure: `test_login_returns_token` expected a 200
response but received 401. The token endpoint appears to reject valid credentials.
```

## Example: Verification File (shell command)

The following is an example of a verification file that instructs the verification agent to run a shell command and report on its output:

```markdown
# Verification: Unit Tests

Run `pytest` in the workspace root and check whether all tests pass.

If any tests fail, include the test names and assertion errors in your reasoning
so the implementation agent can identify and fix them.
```

## Example: Verification File (qualitative check)

The following is an example of a verification file that instructs the verification agent to perform a qualitative assessment:

```markdown
# Verification: Faithfulness to Task

Read the task instructions and review the changes made to the workspace. Assess
whether the implementation faithfully addresses what was asked — no more and
no less.

A faithful implementation:
- Completes all requirements stated in the task.
- Does not introduce unrequested features, structural changes, or refactors.
- Does not leave scaffolding, placeholder code, or TODO comments where real
  implementation was expected.
```

## Example: Verification Prompt (as seen by the verification agent)

For each verification, the system reproduces the verification file content, provides a reference to the task file, and appends a structured response instruction:

```
# Verification: Faithfulness to Task

Read the task instructions and review the changes made to the workspace. Assess
whether the implementation faithfully addresses what was asked — no more and
no less.

A faithful implementation:
- Completes all requirements stated in the task.
- Does not introduce unrequested features, structural changes, or refactors.
- Does not leave scaffolding, placeholder code, or TODO comments where real
  implementation was expected.

---

The task instructions are in `.agent-context/task/TASK.md`. Read them before
making your assessment.

Respond with a single JSON object on the last line of your output. Do not include
any text after the JSON object.

{ "status": "PASS" | "FAIL", "reasoning": "<brief explanation>" }
```

An example agent response to this prompt:

```
I'll read the task instructions and then review the workspace changes.

[... agent reasoning and file reads ...]

The task asked for a login endpoint returning a token. The implementation includes
that, but also adds an unrequested password reset flow and two helper utilities
not mentioned anywhere in the instructions.

{ "status": "FAIL", "reasoning": "Implementation includes an unrequested password reset flow and helper utilities not mentioned in the task." }
```
