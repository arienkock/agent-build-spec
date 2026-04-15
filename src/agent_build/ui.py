from __future__ import annotations

import threading
import subprocess
import time
import sys
from typing import Optional
from pathlib import Path
from .events import (
    AgentEvent,
    AgentOutput,
    AgentProgress,
    AgentStarted,
    AgentCompleted,
    AgentTimedOut,
)


class OutputManager:
    def __init__(self, mode: str, base_commit: str, project_root: Path):
        self.mode = mode
        self.base_commit = base_commit
        self.project_root = project_root
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lines: list[str] = [""]
        self.max_lines = 15
        self.current_stats: str = ""
        self._lock = threading.Lock()

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._stats_loop, daemon=True)
        self.thread.start()

        if self.mode == "ui":
            sys.stdout.write("\n" * self.max_lines)
            self._redraw()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.0)

    def _stats_loop(self):
        while not self.stop_event.is_set():
            if self.stop_event.wait(timeout=5.0):
                break

            try:
                diff = subprocess.run(
                    ["git", "diff", "--stat", "--color=always", self.base_commit],
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                if diff.returncode == 0 and diff.stdout.strip():
                    stats = diff.stdout.strip()
                    if stats != self.current_stats:
                        self.current_stats = stats
                        self.on_event(AgentProgress(stats=stats))
            except Exception:
                pass

    def on_event(self, event: AgentEvent):
        with self._lock:
            if isinstance(event, AgentStarted):
                pass
            elif isinstance(event, AgentOutput):
                if self.mode == "hidden":
                    return

                # Update lines buffer for UI mode
                chunk = event.chunk
                if not chunk:
                    return

                # Split the chunk, preserving newlines
                parts = chunk.split("\n")

                # Append the first part to the last line
                self.lines[-1] += parts[0]

                # If there are more parts, they represent new lines
                for part in parts[1:]:
                    self.lines.append(part)
                    if len(self.lines) > self.max_lines:
                        self.lines.pop(0)

                if self.mode == "append":
                    print(chunk, end="", flush=True)
                elif self.mode == "ui":
                    self._redraw()
            elif isinstance(event, AgentProgress):
                if self.mode == "append":
                    print(
                        f"\n--- Progress ---\n{event.stats}\n----------------\n",
                        flush=True,
                    )
                elif self.mode == "ui":
                    self._redraw()

    def _redraw(self):
        # Move cursor up max_lines + stats lines (roughly)
        # To keep it simple, we use standard ANSI

        # Clear screen below cursor is \033[J but we want to move up first.
        # It's easier to use \033[{n}A to move cursor up.

        # calculate how many lines to move up.
        stats_lines = len(self.current_stats.splitlines()) if self.current_stats else 0
        total_lines = self.max_lines + stats_lines + 2

        sys.stdout.write(f"\033[{total_lines}A\033[J")

        for i in range(self.max_lines):
            if i < len(self.lines):
                sys.stdout.write(f"{self.lines[i]}\033[K\n")
            else:
                sys.stdout.write("\033[K\n")

        sys.stdout.write("\n\033[K")
        sys.stdout.write(f"--- Git Stats ---\n{self.current_stats}\033[K\n")
        sys.stdout.flush()
