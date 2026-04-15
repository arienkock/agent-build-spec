import json
import sys
from pathlib import Path
from agent_build.config import Config
from agent_build.task_run import run_task
from agent_build.results import ResultsStore
from agent_build.types import Task

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


def test_fresh_session_on_timeout(tmp_path):
    """
    Verifies that when an implementation agent call times out, the next retry
    starts with a fresh session (no session_id), even if the timed-out call
    had provided a session_id.

    This is desirable because fresh sessions often perform better for
    completing complex tasks when an agent has become stuck or confused
    in its current session.
    """
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "src").mkdir()
    (project_root / "tasks").mkdir()
    (project_root / "verifications").mkdir()

    task_dir = project_root / "tasks" / "001-task"
    task_dir.mkdir()
    (task_dir / "TASK.md").write_text("# Task 1")

    # We need a counter to vary behavior between invocations
    count_file = tmp_path / "invocations.txt"

    # Agent command:
    # 1st call: provides session_id 'S1' but sleeps long enough to timeout
    # 2nd call: (retry) should NOT have {session_id} replaced with 'S1'
    #           because we reset it. We check this by making fake_agent
    #           fail if it sees a session ID in its arguments when we expect none.

    # We'll use a wrapper script or just shlex-compatible args in the config.
    # Note: run_agent replaces {session_id} with "" if it's None.

    cmd = (
        f"{sys.executable} {FAKE_AGENT} "
        f"--invocation-count-file {count_file} "
        f"--model {{model}} "
        f"{{prompt}} "
        f"--session-id S1 "
        f"--sleep 2"  # Timeout is 1s
    )

    # We use a separate command for resume to detect if it was called
    resume_cmd = (
        f"{sys.executable} {FAKE_AGENT} "
        f"--invocation-count-file {count_file} "
        f"--model {{model}} "
        f"RESUME_MARKER "
        f"{{prompt}} "
        f"--session-id S1"
    )

    config = Config(
        agent_command=cmd,
        agent_resume_command=resume_cmd,
        model="test-model",
        agent_timeout_seconds=1,  # Trigger timeout
        verification_timeout_seconds=10,
        max_retries=1,  # Allow one retry
    )

    store = ResultsStore(project_root / "results")
    task = Task(id="001-task", path=task_dir)

    # We expect run_task to eventually fail because both attempts timeout
    # or the second one succeeds but we want to check the invocation.
    # Actually, let's make the 2nd attempt succeed and check if it was a resume.

    # Update fake_agent behavior via the count file:
    # We can't easily change the cmd mid-run, but we can make the fake_agent
    # exit early on the second call.

    cmd_with_conditional_sleep = (
        f"{sys.executable} {FAKE_AGENT} "
        f"--invocation-count-file {count_file} "
        f"--model {{model}} "
        f"{{prompt}} "
        f"--session-id S1 "
        f"--fail-if-stdin-contains RESUME_MARKER "  # Should NOT be a resume
    )

    # If the second call is a resume, it will use agent_resume_command which has RESUME_MARKER.
    # If it's a fresh start, it uses agent_command.

    # Wait, I need the first call to timeout.
    # Let's use a small python snippet in the command to sleep only on first call.

    sleep_logic = (
        "import sys, pathlib; "
        "p = pathlib.Path('invocations.txt'); "
        "count = int(p.read_text()) if p.exists() else 0; "
        "count += 1; p.write_text(str(count)); "
        "import time; "
        "if count == 1: time.sleep(2)"
    )

    # Simplified approach:
    # Use the invocation count file we already have.
    # We'll point the agent_command to a script that sleeps on call 1.

    agent_script = tmp_path / "agent_logic.py"
    agent_script.write_text(f"""
import sys, pathlib, time, json
count_file = pathlib.Path('{count_file}')
count = int(count_file.read_text()) if count_file.exists() else 0
# The task runner increments count via fake_agent --invocation-count-file, 
# but here we just want to sleep on the first invocation.
# Actually, run_agent calls the command.

if "RESUME_MARKER" in sys.argv:
    # This should NOT happen on retry after timeout
    pathlib.Path('{tmp_path}/resumed.txt').write_text("RESUMED")
    sys.exit(0)

# We use the count to sleep on the first implementation call
call_count_file = pathlib.Path('{tmp_path}/calls.txt')
calls = int(call_count_file.read_text()) if call_count_file.exists() else 0
calls += 1
call_count_file.write_text(str(calls))

if calls == 1:
    time.sleep(2)
else:
    # Success on second call
    # Output session info to see if runner tries to use it next time (it shouldn't here)
    print(json.dumps({{"type": "session", "session_id": "S1"}}))
    sys.exit(0)
""")

    config = Config(
        agent_command=f"{sys.executable} {agent_script}",
        agent_resume_command=f"{sys.executable} {agent_script} RESUME_MARKER",
        model="test-model",
        agent_timeout_seconds=1,
        verification_timeout_seconds=10,
        max_retries=1,
    )

    import subprocess

    # Initialize git for preflight
    subprocess.run(["git", "init"], cwd=project_root)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=project_root
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=project_root)
    subprocess.run(["git", "add", "."], cwd=project_root)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_root)

    run_task(project_root, task, store, config, yes=True)

    # Verify that RESUME_MARKER was NEVER seen
    assert not (tmp_path / "resumed.txt").exists(), (
        "Session was resumed after timeout, but it should have been a fresh start."
    )

    # Verify we actually had two calls
    call_count = int((tmp_path / "calls.txt").read_text())
    assert call_count == 2
