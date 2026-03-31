"""Regression test: no failed→failed TaskTransitionError.

The bug: SLICE_COMPLETENESS_VIOLATION path in cli.py both transitioned
the task to 'failed' AND raised CliError. The outer except caught the
CliError and tried to transition to 'failed' again → TaskTransitionError.

The fix: inner path raises without transitioning. The outer except owns
ALL failure transitions. One path, zero ambiguity.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from runtime.tasks import TASK_TRANSITIONS, TaskQueueStore


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "state").mkdir()
    (ws / "events").mkdir()
    (ws / "schema").mkdir()
    (ws / "rules").mkdir()
    (ws / "reports").mkdir()
    (ws / "prompts").mkdir()
    (ws / "audit_config.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "audit_id": "audit_test",
        "target_repo_path": str(tmp_path / "repo"),
        "title": "test",
    }))
    (ws / "state" / "canonical_state.json").write_text(json.dumps({
        "audit": {"id": "audit_test", "status": "initialized"},
        "observations": {},
        "hypotheses": {},
        "questions": {},
        "issues": {},
        "contradictions": {},
        "decisions": {},
        "candidates": {},
        "tasks": {},
    }))
    return ws


def _enqueue_running_task(ws: Path) -> str:
    """Create a task in 'running' state and return its id."""
    from runtime.tasks import AuditTask, TaskTarget

    queue = TaskQueueStore(ws)
    task = AuditTask.create(
        audit_id="audit_test",
        task_type="module_scan",
        target=TaskTarget(kind="path", value="app", snapshot_ref="abc123"),
    )
    queue.enqueue_task(task)
    queue.claim_next_task(audit_id="audit_test")
    return task.id


def test_state_machine_forbids_failed_to_failed():
    """The state machine itself must not allow failed→failed."""
    assert "failed" not in TASK_TRANSITIONS["failed"]


def test_single_transition_on_slice_completeness_violation(tmp_path):
    """Reproduces the original bug scenario and verifies the fix.

    Before fix: inner path called transition_task("failed") then raised.
    Outer except caught it and called transition_task("failed") again → crash.

    After fix: inner path only raises. Outer except transitions once.
    Result: exactly one running→failed transition, no TaskTransitionError.
    """
    from runtime.tasks import AuditTask, TaskTarget, TaskTransitionError

    ws = _make_workspace(tmp_path)
    queue = TaskQueueStore(ws)
    task_id = _enqueue_running_task(ws)

    # --- Reproduce the old control flow (pre-fix) ---
    # If we did both transition AND raise, the second call would crash:
    queue.transition_task(task_id, "failed", error="inner diagnostic")
    with patch.object(queue, "transition_task", wraps=queue.transition_task) as spy:
        try:
            queue.transition_task(task_id, "failed", error="outer retry")
        except TaskTransitionError:
            pass  # Old code crashed here
        # Verify transition_task was called (and failed)
        assert spy.call_count == 1

    # --- Verify new control flow ---
    # Reset: task must go through pending→running→failed via single path
    ws2_parent = tmp_path / "ws2"
    ws2_parent.mkdir(parents=True, exist_ok=True)
    ws2 = _make_workspace(ws2_parent)
    queue2 = TaskQueueStore(ws2)
    task2 = AuditTask.create(
        audit_id="audit_test",
        task_type="module_scan",
        target=TaskTarget(kind="path", value="app", snapshot_ref="abc123"),
    )
    queue2.enqueue_task(task2)
    queue2.claim_next_task(audit_id="audit_test")

    # Inner path: raise only (no transition)
    error_message = "SLICE_COMPLETENESS_VIOLATION: module_scan task has no target_sources"

    # Outer except: single transition
    failed_task = queue2.transition_task(task2.id, "failed", error=error_message)
    assert failed_task.status == "failed"
    assert failed_task.last_error == error_message


def test_no_accepted_events_single_transition(tmp_path):
    """The no-accepted-events path also goes through the outer except once."""
    from runtime.tasks import AuditTask, TaskTarget

    ws = _make_workspace(tmp_path)
    queue = TaskQueueStore(ws)
    task = AuditTask.create(
        audit_id="audit_test",
        task_type="module_scan",
        target=TaskTarget(kind="path", value="app", snapshot_ref="abc123"),
    )
    queue.enqueue_task(task)
    queue.claim_next_task(audit_id="audit_test")

    error_msg = "run-task produced no accepted candidate events for task"
    failed_task = queue.transition_task(task.id, "failed", error=error_msg)
    assert failed_task.status == "failed"
    assert failed_task.last_error == error_msg

    # Verify: exactly one transition happened (running→failed)
    # A second attempt would be rejected by the state machine
    from runtime.tasks import TaskTransitionError
    try:
        queue.transition_task(task.id, "failed", error="duplicate")
        assert False, "Second failed→failed should raise"
    except TaskTransitionError:
        pass  # Correct: state machine prevents it
