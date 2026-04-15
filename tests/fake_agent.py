#!/usr/bin/env python3
"""
Fake agent for testing agent-build.

Reads stdin, then dispatches to verification mode or agent mode based on
whether the sentinel string "Respond with a JSON object on the last line"
appears in the stdin content.

Agent mode flags:
  --model          Model name (ignored, satisfies {model} placeholder)
  --exit-code      Exit code to return (default 0)
  --create-file    Create a file at this path relative to cwd (src/)

Verification mode flags:
  --verification-fail          Output FAIL JSON (default: PASS)
  --fail-reason STR            Reasoning string for FAIL (default: "Test failure")
  --verification-exit-code N   Exit code in verification mode (default 0)
  --verification-no-output     Print nothing in verification mode
  --verification-raw-output S  Print raw string (non-JSON) in verification mode
  --verification-sleep N       Sleep N seconds before responding (default 0)
  --verification-create-file S Create file relative to cwd in verification mode
  --fail-if-stdin-contains S   FAIL if this string appears anywhere in stdin

Stateful sequential behavior:
  --invocation-count-file PATH  Increment counter at PATH; use count to vary behavior.
                                 Odd invocations → agent mode behavior, even → can differ.
                                 (Tests use this for "pass on first verification, fail on second")
"""

import argparse
import json
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(description="Fake agent for testing")
# Agent mode
parser.add_argument("--model", default="test-model", help="Model name (ignored)")
parser.add_argument(
    "--exit-code",
    type=int,
    default=0,
    dest="exit_code",
    help="Exit code to return in agent mode (default 0)",
)
parser.add_argument(
    "--create-file",
    default=None,
    dest="create_file",
    help="Create a file at this path relative to cwd (src/)",
)
# Verification mode
parser.add_argument(
    "--verification-fail",
    action="store_true",
    dest="verification_fail",
    help="Output FAIL JSON in verification mode",
)
parser.add_argument(
    "--fail-reason",
    default="Test failure",
    dest="fail_reason",
    help="Reasoning string for FAIL response",
)
parser.add_argument(
    "--verification-exit-code",
    type=int,
    default=0,
    dest="verification_exit_code",
    help="Exit code in verification mode (default 0)",
)
parser.add_argument(
    "--verification-no-output",
    action="store_true",
    dest="verification_no_output",
    help="Print nothing in verification mode",
)
parser.add_argument(
    "--verification-raw-output",
    default=None,
    dest="verification_raw_output",
    help="Print raw (non-JSON) string in verification mode",
)
parser.add_argument(
    "--verification-sleep",
    type=float,
    default=0,
    dest="verification_sleep",
    help="Sleep N seconds before responding in verification mode",
)
parser.add_argument(
    "--verification-create-file",
    default=None,
    dest="verification_create_file",
    help="Create file relative to cwd in verification mode",
)
parser.add_argument(
    "--fail-if-stdin-contains",
    default=None,
    dest="fail_if_stdin_contains",
    help="FAIL if this string appears anywhere in stdin",
)
parser.add_argument(
    "--invocation-count-file",
    default=None,
    dest="invocation_count_file",
    help="Path to counter file for stateful sequential behavior",
)
parser.add_argument(
    "--session-id",
    default=None,
    dest="session_id",
    help="Session ID to return in JSON output",
)
parser.add_argument(
    "--create-file-on-prompt",
    default=None,
    dest="create_file_on_prompt",
    help="Prompt string that triggers file creation",
)
parser.add_argument(
    "--create-file-path",
    default=None,
    dest="create_file_path",
    help="Path of the file to create when create-file-on-prompt matches",
)
parser.add_argument("prompt", nargs="?", default="", help="Prompt passed to the agent")
args, unknown = parser.parse_known_args()

stdin_content = args.prompt

# Track invocation count if requested
invocation_num = 1
if args.invocation_count_file:
    count_path = Path(args.invocation_count_file)
    if count_path.exists():
        try:
            invocation_num = int(count_path.read_text().strip()) + 1
        except ValueError:
            invocation_num = 1
    count_path.write_text(str(invocation_num))

# Detection: are we being called as a verification agent?
IS_VERIFICATION = "Respond with a JSON object on the last line" in stdin_content

if IS_VERIFICATION:
    if args.verification_sleep > 0:
        time.sleep(args.verification_sleep)

    if args.verification_create_file:
        p = Path(args.verification_create_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("verification marker\n")

    if args.verification_no_output:
        sys.exit(args.verification_exit_code)

    if args.verification_raw_output is not None:
        print(args.verification_raw_output)
        sys.exit(args.verification_exit_code)

    # Determine PASS or FAIL
    should_fail = args.verification_fail

    # --fail-if-stdin-contains overrides --verification-fail when the string is found
    if args.fail_if_stdin_contains and args.fail_if_stdin_contains in stdin_content:
        should_fail = True

    if should_fail:
        print(json.dumps({"status": "FAIL", "reasoning": args.fail_reason}))
    else:
        print(json.dumps({"status": "PASS", "reasoning": "ok"}))

    sys.exit(args.verification_exit_code)

else:
    # Agent mode: optionally create a file, then exit with the configured code
    if args.session_id:
        print(json.dumps({"type": "session", "session_id": args.session_id}))

    if args.create_file:
        p = Path(args.create_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fake agent output\n")

    if args.create_file_on_prompt:
        found = args.create_file_on_prompt in stdin_content
        if not found:
            for u in unknown:
                if args.create_file_on_prompt in u:
                    found = True
                    break
        if found:
            if args.create_file_path:
                p = Path(args.create_file_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("created on review prompt\n")

    sys.exit(args.exit_code)
