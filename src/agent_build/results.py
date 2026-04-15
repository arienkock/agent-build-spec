from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from .types import ResultRecord, TaskRunStatus

_LATEST_RE = re.compile(r"^results-(.+)\.json$")
_ARCHIVED_RE = re.compile(r"^results-(.+)--run-(\d+)\.json$")


class ResultsStoreError(Exception):
    pass


class ResultsStore:
    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(record: ResultRecord) -> dict:
        if record.status == TaskRunStatus.SKIPPED:
            return {
                "status": record.status.value,
                "previousResults": record.previous_results,
            }
        d: dict = {
            "status": record.status.value,
            "previousResults": record.previous_results,
        }
        if record.base_commit is not None:
            d["baseCommit"] = record.base_commit
        if record.start_time is not None:
            d["startTime"] = record.start_time
        if record.end_time is not None:
            d["endTime"] = record.end_time
        if record.cpu_user_time is not None:
            d["cpuUserTime"] = record.cpu_user_time
        if record.cpu_system_time is not None:
            d["cpuSystemTime"] = record.cpu_system_time
        if record.io_time is not None:
            d["ioTime"] = record.io_time
        if record.input_tokens is not None:
            d["inputTokens"] = record.input_tokens
        if record.output_tokens is not None:
            d["outputTokens"] = record.output_tokens
        if record.cost is not None:
            d["cost"] = record.cost
        return d

    @staticmethod
    def _from_dict(data: dict) -> ResultRecord:
        status_raw = data.get("status")
        if status_raw is None:
            raise ResultsStoreError("Result record is missing required 'status' field")
        try:
            status = TaskRunStatus(status_raw)
        except ValueError as exc:
            raise ResultsStoreError(f"Unknown status value: {status_raw!r}") from exc

        return ResultRecord(
            status=status,
            previous_results=data.get("previousResults"),
            base_commit=data.get("baseCommit"),
            start_time=data.get("startTime"),
            end_time=data.get("endTime"),
            cpu_user_time=data.get("cpuUserTime"),
            cpu_system_time=data.get("cpuSystemTime"),
            io_time=data.get("ioTime"),
            input_tokens=data.get("inputTokens"),
            output_tokens=data.get("outputTokens"),
            cost=data.get("cost"),
        )

    # ------------------------------------------------------------------
    # File path helpers
    # ------------------------------------------------------------------

    def _latest_path(self, task_id: str) -> Path:
        return self.results_dir / f"results-{task_id}.json"

    def _archived_path(self, task_id: str, order: int) -> Path:
        return self.results_dir / f"results-{task_id}--run-{order}.json"

    def _next_archive_order(self, task_id: str) -> int:
        """Return max_existing_archived_order + 1 (or 1 if none exist)."""
        max_order = 0
        if self.results_dir.exists():
            for p in self.results_dir.iterdir():
                m = _ARCHIVED_RE.match(p.name)
                if m and m.group(1) == task_id:
                    max_order = max(max_order, int(m.group(2)))
        return max_order + 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_latest(self, task_id: str) -> Optional[ResultRecord]:
        """
        Return the latest ResultRecord for task_id, or None if no record exists.
        Raises ResultsStoreError on malformed/missing-status JSON.
        """
        path = self._latest_path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ResultsStoreError(f"Malformed JSON in {path.name}: {exc}") from exc
        return self._from_dict(data)

    def task_ids_in_results(self) -> set[str]:
        """Return all task IDs referenced by any results file (latest or archived)."""
        ids: set[str] = set()
        if not self.results_dir.exists():
            return ids
        for p in self.results_dir.iterdir():
            # Check archived pattern first — it is more specific than latest.
            # _LATEST_RE is greedy and would match archived filenames too.
            m = _ARCHIVED_RE.match(p.name)
            if m:
                ids.add(m.group(1))
                continue
            m = _LATEST_RE.match(p.name)
            if m:
                ids.add(m.group(1))
        return ids

    def check_consistency(self) -> set[str]:
        """
        Return task IDs that have an archived record but no latest record.
        An empty set means the store is consistent.
        """
        if not self.results_dir.exists():
            return set()

        has_latest: set[str] = set()
        has_archived: set[str] = set()

        for p in self.results_dir.iterdir():
            # Check archived pattern first — it is more specific than latest.
            m = _ARCHIVED_RE.match(p.name)
            if m:
                has_archived.add(m.group(1))
                continue
            m = _LATEST_RE.match(p.name)
            if m:
                has_latest.add(m.group(1))

        return has_archived - has_latest

    def next_archive_filename(self, task_id: str) -> Optional[str]:
        """
        Return the filename the current latest record will be archived as,
        or None if there is no current latest record.
        """
        if not self._latest_path(task_id).exists():
            return None
        order = self._next_archive_order(task_id)
        return f"results-{task_id}--run-{order}.json"

    def write(self, task_id: str, record: ResultRecord) -> None:
        """
        Write record as the latest result for task_id.
        Creates results_dir if absent. Archives the existing latest atomically.
        """
        self.results_dir.mkdir(parents=True, exist_ok=True)
        latest_path = self._latest_path(task_id)

        # Archive existing latest record first
        if latest_path.exists():
            order = self._next_archive_order(task_id)
            archive_path = self._archived_path(task_id, order)
            latest_path.rename(archive_path)

        # Write new record atomically: temp file → rename
        data = json.dumps(self._to_dict(record), indent=2)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=self.results_dir, prefix=f"results-{task_id}-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp_path_str, latest_path)
        except Exception:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise
