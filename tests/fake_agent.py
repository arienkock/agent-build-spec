#!/usr/bin/env python3
"""
Fake agent for testing agent-build.

Reads (and discards) stdin, then exits with the given exit code.
Accepts --model to satisfy the {model} placeholder in agent_command.
Optionally creates a file at the given path relative to cwd (src/).
"""
import argparse
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description="Fake agent for testing")
parser.add_argument("--model", default="test-model", help="Model name (ignored)")
parser.add_argument("--exit-code", type=int, default=0, dest="exit_code",
                    help="Exit code to return (default 0)")
parser.add_argument("--create-file", default=None, dest="create_file",
                    help="Create a file at this path relative to cwd (src/)")
args = parser.parse_args()

# Read and discard stdin (the prompt)
sys.stdin.read()

# Optionally create a file to simulate agent work
if args.create_file:
    p = Path(args.create_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("fake agent output\n")

sys.exit(args.exit_code)
