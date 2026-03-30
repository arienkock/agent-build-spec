from __future__ import annotations

from .results import ResultsStore, ResultsStoreError
from .types import ResumePoint, ResumePointKind, Task, TaskRunStatus

_SUCCESS_STATUSES = {TaskRunStatus.COMPLETED, TaskRunStatus.SKIPPED}


def determine_resume_point(tasks: list[Task], store: ResultsStore) -> ResumePoint:
    """
    Determine the unambiguous resume point for the given ordered task list.

    Returns a ResumePoint with kind:
      READY           — a specific task is ready to run (task field set)
      COMPLETE        — all tasks have successful records; nothing to run
      NEEDS_CONFIRMATION — last recorded task is 'running' (task field set)
      ERROR           — ambiguous state; message field describes the problem
    """
    task_ids = {t.id for t in tasks}

    # 1. Discrepancy check: IDs in results/ not present in tasks/
    results_ids = store.task_ids_in_results()
    unknown_ids = results_ids - task_ids
    if unknown_ids:
        listed = ", ".join(sorted(unknown_ids))
        return ResumePoint(
            kind=ResumePointKind.ERROR,
            message=(
                f"Results directory contains records for unknown task ID(s): {listed}. "
                "These task directories no longer exist on disk. "
                "Resolve the discrepancy manually before proceeding."
            ),
        )

    # 2. Consistency check: archived record without a latest record
    inconsistent = store.check_consistency()
    if inconsistent:
        listed = ", ".join(sorted(inconsistent))
        return ResumePoint(
            kind=ResumePointKind.ERROR,
            message=(
                f"Task(s) have archived records but no latest record: {listed}. "
                "The results directory is in an inconsistent state."
            ),
        )

    # Collect latest records (raises ResultsStoreError on malformed JSON → ERROR)
    latest: dict[str, object] = {}  # task_id → ResultRecord | None
    for task in tasks:
        try:
            record = store.get_latest(task.id)
        except ResultsStoreError as exc:
            return ResumePoint(
                kind=ResumePointKind.ERROR,
                message=str(exc),
            )
        latest[task.id] = record

    # 3. Multiple running tasks → ERROR
    running_tasks = [t for t in tasks if latest[t.id] is not None and latest[t.id].status == TaskRunStatus.RUNNING]  # type: ignore[union-attr]
    if len(running_tasks) > 1:
        listed = ", ".join(t.id for t in running_tasks)
        return ResumePoint(
            kind=ResumePointKind.ERROR,
            message=f"Multiple tasks have status 'running': {listed}. State is ambiguous.",
        )

    # 4. No records at all → READY (first task)
    tasks_with_records = [t for t in tasks if latest[t.id] is not None]
    if not tasks_with_records:
        return ResumePoint(kind=ResumePointKind.READY, task=tasks[0])

    # 5. Find last_task: highest-ordered task with a latest record
    last_task = tasks_with_records[-1]
    last_task_index = tasks.index(last_task)

    # 6. All tasks before last_task must be completed or skipped
    for task in tasks[:last_task_index]:
        record = latest[task.id]
        if record is None:
            return ResumePoint(
                kind=ResumePointKind.ERROR,
                message=(
                    f"Task '{task.id}' has no record but a later task "
                    f"('{last_task.id}') does. State is ambiguous."
                ),
            )
        if record.status not in _SUCCESS_STATUSES:  # type: ignore[union-attr]
            return ResumePoint(
                kind=ResumePointKind.ERROR,
                message=(
                    f"Task '{task.id}' has status '{record.status.value}' but a later task "  # type: ignore[union-attr]
                    f"('{last_task.id}') has a record. State is ambiguous."
                ),
            )

    # 7. Determine resume point based on last_task status
    last_record = latest[last_task.id]
    assert last_record is not None  # guaranteed by tasks_with_records being non-empty

    if last_record.status == TaskRunStatus.RUNNING:  # type: ignore[union-attr]
        return ResumePoint(kind=ResumePointKind.NEEDS_CONFIRMATION, task=last_task)

    if last_record.status == TaskRunStatus.FAILED:  # type: ignore[union-attr]
        return ResumePoint(kind=ResumePointKind.READY, task=last_task)

    # completed or skipped
    next_index = last_task_index + 1
    if next_index >= len(tasks):
        return ResumePoint(kind=ResumePointKind.COMPLETE)
    return ResumePoint(kind=ResumePointKind.READY, task=tasks[next_index])
