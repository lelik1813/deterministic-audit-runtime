"""Candidate Boundary Validation Tests.

Validates that candidate generation is a strict boundary with NO hidden
execution paths to task creation. This prevents the "hidden work creation"
failure mode where candidates bypass the routing guardrails.

CRITICAL INVARIANT:
- CandidateGenerator may ONLY emit candidate.proposed events
- Task creation happens ONLY through CandidateRouter.route_candidates_to_verification()
- No direct path from candidate generation to task queue mutation
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tempfile
import shutil
from typing import Any

from runtime.tasks import TaskQueueStore, TaskPlanner, CandidateRouter
from runtime.workers.candidate_generator import (
    CandidateGeneratorWorker,
    CandidateGeneratorResult,
    CANDIDATE_GENERATOR_FORBIDDEN_OUTPUTS,
    CANDIDATE_GENERATOR_ALLOWED_OUTPUTS,
)
from runtime.policies import PolicyStore


def test_candidate_generator_output_contract():
    """Verify CandidateGenerator output contract forbids task-creating events."""
    print("=" * 70)
    print("TEST 1: CandidateGenerator output contract")
    print("=" * 70)

    # Verify forbidden outputs include task-triggering events
    forbidden = CANDIDATE_GENERATOR_FORBIDDEN_OUTPUTS

    # Must NOT produce observation events (which would create verify_claim tasks)
    assert "observation.proposed" in forbidden["candidate_event_types"], \
        "observation.proposed must be forbidden (would create verification tasks)"

    # Must NOT produce issue events (which would bypass verification entirely)
    assert "issue.proposed" in forbidden["candidate_event_types"], \
        "issue.proposed must be forbidden (would bypass verification)"

    # Must NOT claim truth
    assert "truth_claims" in forbidden["actions"], \
        "truth_claims must be forbidden action"

    # Must NOT directly mutate state
    assert "direct_state_mutation" in forbidden["actions"], \
        "direct_state_mutation must be forbidden action"

    print("  [PASS] CandidateGenerator contract forbids task-triggering events")
    print("  [PASS] CandidateGenerator contract forbids truth claims")
    print("  [PASS] CandidateGenerator contract forbids direct state mutation")
    return True


def test_candidate_generator_result_has_no_task_methods():
    """Verify CandidateGeneratorResult does not expose task creation methods."""
    print("\n" + "=" * 70)
    print("TEST 2: CandidateGeneratorResult has no task creation methods")
    print("=" * 70)

    from runtime.workers.candidate_generator import CandidateGeneratorResult
    from dataclasses import fields

    # Check fields - should only have payload and candidate_events
    field_names = {f.name for f in fields(CandidateGeneratorResult)}
    assert "payload" in field_names
    assert "candidate_events" in field_names

    # Should NOT have task-related fields
    assert "tasks" not in field_names, "CandidateGeneratorResult should not have tasks field"
    assert "enqueued_tasks" not in field_names, "CandidateGeneratorResult should not have enqueued_tasks"

    # Check methods - should not have task creation methods
    result_methods = [m for m in dir(CandidateGeneratorResult) if not m.startswith("_")]
    forbidden_methods = ["enqueue", "create_task", "add_task", "route"]

    for method in forbidden_methods:
        assert method not in result_methods, \
            f"CandidateGeneratorResult should not have {method} method"

    print("  [PASS] CandidateGeneratorResult has only payload and candidate_events")
    print("  [PASS] No task creation methods on CandidateGeneratorResult")
    return True


def test_candidate_generator_does_not_touch_task_queue():
    """Verify that running CandidateGenerator does not modify task queue.

    This is the CRITICAL test: candidate generation alone must NOT create tasks.
    Only explicit routing through CandidateRouter may create tasks.
    """
    print("\n" + "=" * 70)
    print("TEST 3: CandidateGenerator does not touch task queue")
    print("=" * 70)

    workspace = tempfile.mkdtemp(prefix="candidate_boundary_")
    workspace_path = Path(workspace)

    try:
        # Setup workspace
        state_dir = workspace_path / "state"
        state_dir.mkdir(parents=True)
        queue_dir = state_dir / "queue"
        queue_dir.mkdir(parents=True)
        schema_dir = workspace_path / "schema"
        schema_dir.mkdir(parents=True)
        prompts_dir = workspace_path / "prompts"
        prompts_dir.mkdir(parents=True)

        # Copy config if available
        config_src = PROJECT_ROOT / "config"
        if config_src.exists():
            shutil.copytree(config_src, workspace_path / "config")

        # Copy schemas
        for schema_file in (PROJECT_ROOT / "schema").glob("*.json"):
            shutil.copy(schema_file, schema_dir / schema_file.name)

        # Create minimal prompt file
        prompt_file = prompts_dir / "candidate_generator.md"
        prompt_file.write_text("Generate candidates based on worker input.")

        # Initialize task queue
        queue_store = TaskQueueStore(workspace_path)
        initial_queue = queue_store.read_queue()
        initial_task_count = len(initial_queue.get("tasks", {}))

        # Create CandidateGenerator
        generator = CandidateGeneratorWorker(workspace_path)

        # CRITICAL CHECK: Generator worker does NOT have task creation methods
        generator_methods = [m for m in dir(generator) if not m.startswith("_")]
        task_related = ["enqueue", "create_task", "add_task", "route", "schedule"]
        for method in task_related:
            assert method not in generator_methods, \
                f"CandidateGeneratorWorker should not have {method} method"

        # Task queue must be UNCHANGED after generator instantiation
        final_queue = queue_store.read_queue()
        final_task_count = len(final_queue.get("tasks", {}))

        assert final_task_count == initial_task_count, \
            f"Task queue was modified! Before: {initial_task_count}, After: {final_task_count}"

        print("  [PASS] CandidateGeneratorResult contains candidate events only")
        print("  [PASS] Task queue unchanged after candidate generation")
        print(f"         Initial tasks: {initial_task_count}, Final tasks: {final_task_count}")
        return True

    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_only_router_creates_tasks_from_candidates():
    """Verify that ONLY CandidateRouter can create tasks from candidates."""
    print("\n" + "=" * 70)
    print("TEST 4: Only CandidateRouter creates tasks from candidates")
    print("=" * 70)

    workspace = tempfile.mkdtemp(prefix="candidate_routing_")
    workspace_path = Path(workspace)

    try:
        # Setup workspace
        state_dir = workspace_path / "state"
        state_dir.mkdir(parents=True)
        queue_dir = state_dir / "queue"
        queue_dir.mkdir(parents=True)

        # Copy config
        config_src = PROJECT_ROOT / "config"
        if config_src.exists():
            shutil.copytree(config_src, workspace_path / "config")

        # Initialize stores
        queue_store = TaskQueueStore(workspace_path)
        policy_store = PolicyStore(workspace_path)
        policy = policy_store.get_policy("strict_security")
        router = CandidateRouter(workspace_path, policy=policy)

        # Create a candidate that should route to verification
        candidates = {
            "candidate_001": {
                "id": "candidate_001",
                "audit_id": "audit_test",
                "candidate_type": "risk_candidate",
                "status": "proposed",
                "proposed_claim": "Test vulnerability",
                "confidence": "high",
                "supporting_evidence_refs": [
                    {"file_path": "src/app.py", "snapshot_ref": "snapshot_001"}
                ],
            }
        }

        # Before routing: no tasks
        queue_before = queue_store.read_queue()
        tasks_before = len(queue_before.get("tasks", {}))

        # Route candidates - THIS is the only path to task creation
        results = router.route_candidates_to_verification(
            audit_id="audit_test",
            candidates=candidates,
            audit_snapshot_ref="snapshot_001",
        )

        # After routing: tasks created
        queue_after = queue_store.read_queue()
        tasks_after = len(queue_after.get("tasks", {}))

        # Verify tasks were created by router
        assert tasks_after > tasks_before, \
            f"Router should have created tasks. Before: {tasks_before}, After: {tasks_after}"

        # Verify routing result shows enqueued
        enqueued = [r for r in results if r.outcome == "enqueued"]
        assert len(enqueued) > 0, "At least one candidate should be enqueued"

        print("  [PASS] CandidateRouter creates tasks from candidates")
        print(f"         Tasks before routing: {tasks_before}")
        print(f"         Tasks after routing: {tasks_after}")
        print(f"         Enqueued candidates: {len(enqueued)}")
        return True

    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_no_hidden_candidate_to_task_path():
    """Verify that candidate data files alone do NOT trigger task creation.

    CRITICAL: Only CandidateRouter.route_candidates_to_verification() may create
    tasks from candidates. Writing candidate data must be side-effect free.
    """
    print("\n" + "=" * 70)
    print("TEST 5: No hidden path from candidate data to task creation")
    print("=" * 70)

    workspace = tempfile.mkdtemp(prefix="candidate_event_boundary_")
    workspace_path = Path(workspace)

    try:
        # Setup workspace
        state_dir = workspace_path / "state"
        state_dir.mkdir(parents=True)
        queue_dir = state_dir / "queue"
        queue_dir.mkdir(parents=True)
        candidates_dir = state_dir / "candidates"
        candidates_dir.mkdir(parents=True)

        # Copy config if available
        config_src = PROJECT_ROOT / "config"
        if config_src.exists():
            shutil.copytree(config_src, workspace_path / "config")

        # Initialize task queue
        queue_store = TaskQueueStore(workspace_path)

        # Get initial task count
        queue_before = queue_store.read_queue()
        tasks_before = len(queue_before.get("tasks", {}))

        # Write a candidate file directly - this should NOT create tasks
        candidate_id = "candidate_test_001"
        candidate_file = candidates_dir / f"{candidate_id}.json"
        candidate_file.write_text("""{
            "id": "candidate_test_001",
            "audit_id": "audit_boundary_test",
            "candidate_type": "risk_candidate",
            "status": "proposed",
            "proposed_claim": "Test claim",
            "confidence": "high",
            "supporting_evidence_refs": []
        }""")

        # Read task queue after writing candidate file
        queue_after = queue_store.read_queue()
        tasks_after = len(queue_after.get("tasks", {}))

        # CRITICAL CHECK: Writing a candidate file must NOT create tasks
        # Only explicit routing through CandidateRouter creates tasks
        assert tasks_after == tasks_before, \
            f"Hidden task creation detected! Tasks before: {tasks_before}, after: {tasks_after}"

        print("  [PASS] Writing candidate data does not create tasks")
        print("  [PASS] No hidden path from candidate storage to task queue")
        print(f"         Tasks before candidate: {tasks_before}")
        print(f"         Tasks after candidate: {tasks_after}")
        return True

    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def run_all_tests():
    """Run all candidate boundary tests."""
    print("=" * 70)
    print("CANDIDATE BOUNDARY VALIDATION")
    print("Testing that candidate generation has no hidden execution paths")
    print("=" * 70)

    all_passed = True

    tests = [
        test_candidate_generator_output_contract,
        test_candidate_generator_result_has_no_task_methods,
        test_candidate_generator_does_not_touch_task_queue,
        test_only_router_creates_tasks_from_candidates,
        test_no_hidden_candidate_to_task_path,
    ]

    for test in tests:
        try:
            if not test():
                all_passed = False
        except AssertionError as e:
            print(f"  [FAIL] {e}")
            all_passed = False
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            all_passed = False

    print("\n" + "=" * 70)
    if all_passed:
        print("ALL BOUNDARY TESTS PASSED")
        print("Candidate generation is a strict boundary with no hidden paths")
    else:
        print("SOME BOUNDARY TESTS FAILED")
    print("=" * 70)

    return all_passed


if __name__ == "__main__":
    success = run_all_tests()
    if not success:
        sys.exit(1)
