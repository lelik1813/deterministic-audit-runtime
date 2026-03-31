"""Unit tests for task identity layer and idempotent enqueue behavior.

Tests verify:
1. task_id is deterministic for semantically equivalent tasks
2. Path normalization is consistent
3. Enqueue is idempotent
4. Semantic identity invariant is enforced
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

# Allow running tests directly without package installation
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.tasks import (
    TASK_STATUSES,
    TASK_TYPES,
    TARGET_KINDS,
    AuditTask,
    EnqueueResult,
    TaskPlanner,
    TaskQueueError,
    TaskQueueStore,
    TaskTarget,
    _task_semantic_key,
    _normalize_target_value,
    build_task_id,
)


class TestTaskIdDeterminism:
    """Verify task_id generation is deterministic and stable."""

    def test_same_inputs_same_id(self):
        """Identical inputs must produce identical task_id."""
        target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
        id1 = build_task_id("audit_test", "module_scan", target)
        id2 = build_task_id("audit_test", "module_scan", target)
        assert id1 == id2
        assert id1.startswith("task_")

    def test_different_audit_id_different_task_id(self):
        """Different audit_id must produce different task_id."""
        target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
        id1 = build_task_id("audit_a", "module_scan", target)
        id2 = build_task_id("audit_b", "module_scan", target)
        assert id1 != id2

    def test_different_type_different_task_id(self):
        """Different task type must produce different task_id."""
        target = TaskTarget(kind="observation", value="obs_001", snapshot_ref="snap_001")
        id1 = build_task_id("audit_test", "verify_claim", target)
        id2 = build_task_id("audit_test", "compose_issue", target)
        assert id1 != id2

    def test_different_target_value_different_task_id(self):
        """Different target value must produce different task_id."""
        target1 = TaskTarget(kind="path", value="src/a.py", snapshot_ref="snap_001")
        target2 = TaskTarget(kind="path", value="src/b.py", snapshot_ref="snap_001")
        id1 = build_task_id("audit_test", "module_scan", target1)
        id2 = build_task_id("audit_test", "module_scan", target2)
        assert id1 != id2


class TestPathNormalization:
    """Verify path normalization is consistent across task_id and semantic_key."""

    @pytest.mark.parametrize("input_path,expected", [
        ("src/main.py", "src/main.py"),
        ("./src/main.py", "src/main.py"),
        ("src//main.py", "src/main.py"),
        ("src/main.py/", "src/main.py"),
        ("src/./main.py", "src/main.py"),
        (r"src\main.py", "src/main.py"),  # Windows path
        ("  src/main.py  ", "src/main.py"),  # Whitespace
    ])
    def test_normalize_target_value(self, input_path, expected):
        """Path normalization must produce canonical form."""
        result = _normalize_target_value("path", input_path)
        assert result == expected

    def test_normalized_paths_produce_same_task_id(self):
        """Different representations of same path must produce same task_id."""
        paths = [
            "src/main.py",
            "./src/main.py",
            "src//main.py",
            r"src\main.py",
        ]
        task_ids = set()
        for path in paths:
            target = TaskTarget(kind="path", value=path, snapshot_ref="snap_001")
            task_id = build_task_id("audit_test", "module_scan", target)
            task_ids.add(task_id)
        assert len(task_ids) == 1, f"Expected single task_id, got: {task_ids}"

    def test_normalized_paths_produce_same_semantic_key(self):
        """Different representations of same path must produce same semantic_key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            paths = [
                "src/main.py",
                "./src/main.py",
                "src//main.py",
            ]
            keys = set()
            for path in paths:
                target = TaskTarget(kind="path", value=path, snapshot_ref="snap_001")
                task = AuditTask.create(
                    audit_id="audit_test",
                    task_type="module_scan",
                    target=target,
                )
                keys.add(_task_semantic_key(task))
            assert len(keys) == 1, f"Expected single semantic_key, got: {keys}"


class TestTaskTargetNormalization:
    """Verify TaskTarget normalizes values at construction time."""

    def test_path_value_normalized_at_construction(self):
        """TaskTarget must normalize path values at construction."""
        target = TaskTarget(kind="path", value="./src//main.py/", snapshot_ref="snap_001")
        assert target.value == "src/main.py"

    def test_snapshot_ref_trimmed(self):
        """TaskTarget must strip whitespace from snapshot_ref."""
        target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="  snap_001  ")
        assert target.snapshot_ref == "snap_001"

    def test_module_value_normalized(self):
        """TaskTarget must normalize module values (same as path)."""
        target = TaskTarget(kind="module", value="./utils//helpers.py", snapshot_ref="snap_001")
        assert target.value == "utils/helpers.py"

    def test_observation_value_not_path_normalized(self):
        """Observation values should not be path-normalized, only trimmed."""
        target = TaskTarget(kind="observation", value="  obs_abc123  ", snapshot_ref="snap_001")
        assert target.value == "obs_abc123"


class TestIdempotentEnqueue:
    """Verify enqueue_many is idempotent."""

    def test_enqueue_same_task_twice_returns_duplicate(self):
        """Enqueueing same task twice must return duplicate, not error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
            task = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
            )

            result1 = store.enqueue_task(task)
            result2 = store.enqueue_task(task)

            assert result1.outcome == "enqueued"
            assert result2.outcome == "duplicate"
            assert result1.task.id == result2.task.id

    def test_enqueue_different_timestamp_same_idempotent_key(self):
        """Same semantic task created at different times must be idempotent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")

            task1 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
                created_at="2024-01-01T00:00:00Z",
            )
            task2 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
                created_at="2024-01-02T00:00:00Z",
            )

            # task_id is deterministic, so they should have same id
            assert task1.id == task2.id

            result1 = store.enqueue_task(task1)
            result2 = store.enqueue_task(task2)

            assert result1.outcome == "enqueued"
            assert result2.outcome == "duplicate"

    def test_no_duplicates_after_multiple_enqueue_calls(self):
        """Multiple enqueue calls must not create duplicate entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
            task = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
            )

            for _ in range(5):
                store.enqueue_task(task)

            all_tasks = store.list_tasks()
            assert len(all_tasks) == 1


class TestSemanticIdentityInvariant:
    """Verify semantic identity invariant is enforced."""

    def test_task_id_collision_detected(self):
        """If task_id collides but semantic content differs, must raise error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)

            # Create a task and enqueue it
            target1 = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
            task1 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target1,
            )
            store.enqueue_task(task1)

            # Manually create a task with same id but different semantic content
            # This simulates a hash collision or bug
            target2 = TaskTarget(kind="path", value="src/other.py", snapshot_ref="snap_001")
            task2_payload = {
                "id": task1.id,  # Same id
                "audit_id": "audit_test",
                "type": "module_scan",
                "status": "pending",
                "target": target2.to_dict(),  # Different target
                "attempt_count": 0,
                "last_error": None,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
            task2 = AuditTask.from_dict(task2_payload)

            # This must raise because semantic keys differ
            with pytest.raises(TaskQueueError) as exc_info:
                store.enqueue_task(task2)

            assert "collision" in str(exc_info.value).lower()


class TestEnqueueMany:
    """Verify enqueue_many batch behavior."""

    def test_enqueue_many_returns_correct_outcomes(self):
        """enqueue_many must return correct outcomes for each task."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)

            target1 = TaskTarget(kind="path", value="src/a.py", snapshot_ref="snap_001")
            target2 = TaskTarget(kind="path", value="src/b.py", snapshot_ref="snap_001")

            task1 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target1,
            )
            task2 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target2,
            )

            # First batch: both enqueued
            results = store.enqueue_many([task1, task2])
            assert len(results) == 2
            assert all(r.outcome == "enqueued" for r in results)

            # Second batch: both duplicates
            results = store.enqueue_many([task1, task2])
            assert len(results) == 2
            assert all(r.outcome == "duplicate" for r in results)

    def test_enqueue_many_partial_duplicates(self):
        """enqueue_many must handle mix of new and duplicate tasks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)

            target1 = TaskTarget(kind="path", value="src/a.py", snapshot_ref="snap_001")
            target2 = TaskTarget(kind="path", value="src/b.py", snapshot_ref="snap_001")
            target3 = TaskTarget(kind="path", value="src/c.py", snapshot_ref="snap_001")

            task1 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target1,
            )
            task2 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target2,
            )

            # Enqueue first two
            store.enqueue_many([task1, task2])

            # Now enqueue all three: 1,2 are duplicates, 3 is new
            task3 = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target3,
            )
            results = store.enqueue_many([task1, task2, task3])

            outcomes = [r.outcome for r in results]
            assert outcomes.count("duplicate") == 2
            assert outcomes.count("enqueued") == 1


class TestTaskLifecycle:
    """Verify task lifecycle transitions."""

    def test_pending_to_running_to_done(self):
        """Normal task lifecycle: pending -> running -> done."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
            task = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
            )
            store.enqueue_task(task)

            # pending -> running
            running = store.transition_task(task.id, "running")
            assert running.status == "running"
            assert running.attempt_count == 1

            # running -> done
            done = store.transition_task(task.id, "done")
            assert done.status == "done"

    def test_running_to_failed(self):
        """Task can fail: running -> failed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
            task = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
            )
            store.enqueue_task(task)
            store.transition_task(task.id, "running")

            failed = store.transition_task(task.id, "failed", error="Test error")
            assert failed.status == "failed"
            assert failed.last_error == "Test error"

    def test_failed_can_retry(self):
        """Failed task can be retried: failed -> pending."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
            task = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
            )
            store.enqueue_task(task)
            store.transition_task(task.id, "running")
            store.transition_task(task.id, "failed", error="Test error")

            # failed -> pending (retry)
            retry = store.transition_task(task.id, "pending")
            assert retry.status == "pending"

    def test_done_is_terminal(self):
        """Done task cannot transition further."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskQueueStore(tmpdir)
            target = TaskTarget(kind="path", value="src/main.py", snapshot_ref="snap_001")
            task = AuditTask.create(
                audit_id="audit_test",
                task_type="module_scan",
                target=target,
            )
            store.enqueue_task(task)
            store.transition_task(task.id, "running")
            store.transition_task(task.id, "done")

            from runtime.tasks import TaskTransitionError
            with pytest.raises(TaskTransitionError):
                store.transition_task(task.id, "pending")


class TestQueueRecovery:
    """Verify queue recovery behavior."""

    def test_empty_queue_file_created(self):
        """Missing queue file must be created with empty state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = Path(tmpdir) / "state" / "task_queue.json"
            assert not queue_path.exists()

            store = TaskQueueStore(tmpdir)

            assert queue_path.exists()
            assert store.list_tasks() == []

    def test_recover_corrupted_queue(self):
        """Corrupted queue file must be reset to empty during initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = Path(tmpdir) / "state" / "task_queue.json"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            queue_path.write_text("not valid json {{{")

            # TaskQueueStore.__init__ calls ensure_queue_file which calls recover_queue
            # which repairs the corrupted file
            store = TaskQueueStore(tmpdir)

            # Queue should be empty after recovery
            assert store.list_tasks() == []

            # File should now be valid JSON
            import json
            with open(queue_path) as f:
                data = json.load(f)
            assert "schema_version" in data
            assert "tasks" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
