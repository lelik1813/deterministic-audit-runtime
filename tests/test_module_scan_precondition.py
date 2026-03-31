"""Tests for module_scan slice completeness precondition fail-fast.

Validates that when a module_scan task lacks target_sources:
  1. The backend is NOT invoked
  2. The task is marked as failed
  3. A structured diagnostic is emitted (INFRASTRUCTURE_DEFECT)
  4. Non-module_scan tasks are NOT affected
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli import CliError


# ---------------------------------------------------------------------------
# Unit tests: precondition detection logic (mirrors the if-condition in cli.py)
# ---------------------------------------------------------------------------

def _check_precondition(task_type, target_kind, slice_payload):
    """Reproduce the exact precondition check from cli.py."""
    return (
        task_type == "module_scan"
        and target_kind in {"path", "module"}
        and not slice_payload.get("target_sources")
    )


class TestPreconditionDetection:

    def test_empty_target_sources_triggers(self):
        assert _check_precondition("module_scan", "path", {"target_sources": []}) is True

    def test_missing_target_sources_triggers(self):
        assert _check_precondition("module_scan", "path", {}) is True

    def test_populated_target_sources_passes(self):
        payload = {"target_sources": [{"file_path": "a.py", "snapshot_ref": "s", "file_content": "x"}]}
        assert _check_precondition("module_scan", "path", payload) is False

    def test_module_target_kind_checked(self):
        assert _check_precondition("module_scan", "module", {}) is True

    def test_verify_claim_not_checked(self):
        assert _check_precondition("verify_claim", "observation", {}) is False

    def test_compose_issue_not_checked(self):
        assert _check_precondition("compose_issue", "observation", {}) is False

    def test_module_scan_observation_target_not_checked(self):
        assert _check_precondition("module_scan", "observation", {}) is False

    def test_none_target_sources_triggers(self):
        assert _check_precondition("module_scan", "path", {"target_sources": None}) is True


# ---------------------------------------------------------------------------
# Integration-style: verify the precondition path through command_run_task
# ---------------------------------------------------------------------------

def _build_cli_mocks(slice_payload_dict, *, has_target_sources=True):
    """Build all mocks needed to drive command_run_task to the precondition check."""
    mock_task = MagicMock()
    mock_task.id = "task_test001"
    mock_task.type = "module_scan"
    mock_task.audit_id = "audit_test"
    mock_task.target.kind = "path"
    mock_task.target.value = "app/config.py"
    mock_task.target.snapshot_ref = "snap_abc"
    mock_task.target.to_dict.return_value = {
        "kind": "path", "value": "app/config.py", "snapshot_ref": "snap_abc"
    }
    mock_task.to_dict.return_value = {
        "id": "task_test001", "audit_id": "audit_test", "type": "module_scan",
        "status": "done", "target": mock_task.target.to_dict.return_value,
        "attempt_count": 0, "last_error": None,
        "created_at": "1970-01-01T00:00:00Z", "updated_at": "1970-01-01T00:00:00Z",
    }

    mock_queue = MagicMock()
    mock_queue.claim_next_task.return_value = ("task_test001", "fresh")
    mock_queue.get_task.return_value = mock_task
    mock_queue.transition_task.return_value = mock_task

    mock_ledger = MagicMock()
    mock_ledger.start_run.return_value = MagicMock(run_id="run_001")

    mock_snapshot = MagicMock()
    mock_snapshot.snapshot_ref = "snap_abc"

    slice_result = MagicMock()
    slice_result.slice_id = "slice_abc"
    slice_result.slice_fingerprint = "fp_abc"
    slice_result.slice_path = Path("/tmp/slice.json")

    mock_builder = MagicMock()
    mock_builder.write_slice.return_value = slice_result

    mock_adapter = MagicMock()
    if has_target_sources:
        run_result = MagicMock()
        run_result.candidate_events = []
        run_result.invocation_metadata = {}
        run_result.input_digest = "d1"
        run_result.output_digest = "d2"
        run_result.prompt_digest = "d3"
        run_result.raw_output_digest = "d4"
        mock_adapter.run_with_result.return_value = run_result

    return {
        "task": mock_task,
        "queue": mock_queue,
        "ledger": mock_ledger,
        "snapshot": mock_snapshot,
        "slice_result": slice_result,
        "builder": mock_builder,
        "adapter": mock_adapter,
    }


COMMON_PATCHES = [
    "cli.select_task_for_execution",
    "cli.TaskPlanner",
    "cli.select_and_create_adapter",
    "cli.MemorySliceBuilder",
    "cli.RunLedger",
    "cli.TaskQueueStore",
    "cli.bind_task_snapshot",
    "cli.load_workspace_config",
    "cli.load_json",
    "cli.enrich_candidate_events",
    "cli.process_candidate_events",
]


class TestPreconditionFailFast:

    @patch("cli.process_candidate_events")
    @patch("cli.enrich_candidate_events")
    @patch("cli.load_json")
    @patch("cli.select_and_create_adapter")
    @patch("cli.MemorySliceBuilder")
    @patch("cli.RunLedger")
    @patch("cli.TaskQueueStore")
    @patch("cli.bind_task_snapshot")
    @patch("cli.load_workspace_config")
    @patch("cli.TaskPlanner")
    @patch("cli.select_task_for_execution")
    def test_backend_not_called_when_target_sources_empty(
        self, mock_select_task, mock_planner_cls, mock_config,
        mock_bind_snap, mock_queue_cls, mock_ledger_cls,
        mock_builder_cls, mock_select_adapter, mock_load_json,
        mock_enrich, mock_process,
    ):
        """When module_scan slice has no target_sources, backend is NOT invoked."""
        from argparse import Namespace

        mocks = _build_cli_mocks({}, has_target_sources=False)
        mock_task = mocks["task"]

        mock_config.return_value = {
            "audit_id": "audit_test",
            "target_repo_path": str(Path("/tmp/fake")),
            "policy": "low_noise",
        }
        mock_bind_snap.return_value = mocks["snapshot"]
        mock_queue_cls.return_value = mocks["queue"]
        mock_ledger_cls.return_value = mocks["ledger"]
        mock_builder_cls.return_value = mocks["builder"]
        mock_select_adapter.return_value = (MagicMock(value="claude_sdk"), mocks["adapter"])
        mock_select_task.return_value = (mock_task, "fresh")

        # Slice payload WITHOUT target_sources
        mock_load_json.return_value = {
            "worker_role": "Reader",
            "target_paths": ["app/config.py"],
        }

        from cli import command_run_task
        args = Namespace(
            workspace=Path("/tmp/ws"),
            backend="claude",
            timeout_seconds=60,
            allow_dirty_target=False,
            model=None,
        )
        with pytest.raises(CliError, match="SLICE_COMPLETENESS_VIOLATION"):
            command_run_task(args)

        # Backend must NOT have been called
        mocks["adapter"].run_with_result.assert_not_called()
        # Task must be marked as failed
        mocks["queue"].transition_task.assert_called()
        transition_calls = mocks["queue"].transition_task.call_args_list
        failed_call = [c for c in transition_calls if c[0][1] == "failed"]
        assert len(failed_call) >= 1
        assert "INFRASTRUCTURE_DEFECT" in failed_call[0][1].get("error", "")

    @patch("cli.process_candidate_events")
    @patch("cli.enrich_candidate_events")
    @patch("cli.load_json")
    @patch("cli.select_and_create_adapter")
    @patch("cli.MemorySliceBuilder")
    @patch("cli.RunLedger")
    @patch("cli.TaskQueueStore")
    @patch("cli.bind_task_snapshot")
    @patch("cli.load_workspace_config")
    @patch("cli.TaskPlanner")
    @patch("cli.select_task_for_execution")
    def test_diagnostic_recorded_to_run_ledger(
        self, mock_select_task, mock_planner_cls, mock_config,
        mock_bind_snap, mock_queue_cls, mock_ledger_cls,
        mock_builder_cls, mock_select_adapter, mock_load_json,
        mock_enrich, mock_process,
    ):
        """Verify run ledger records the infrastructure defect failure."""
        from argparse import Namespace

        mocks = _build_cli_mocks({}, has_target_sources=False)
        mock_task = mocks["task"]

        mock_config.return_value = {
            "audit_id": "audit_test",
            "target_repo_path": str(Path("/tmp/fake")),
            "policy": "low_noise",
        }
        mock_bind_snap.return_value = mocks["snapshot"]
        mock_queue_cls.return_value = mocks["queue"]
        mock_ledger_cls.return_value = mocks["ledger"]
        mock_builder_cls.return_value = mocks["builder"]
        mock_select_adapter.return_value = (MagicMock(value="claude_sdk"), mocks["adapter"])
        mock_select_task.return_value = (mock_task, "fresh")

        mock_load_json.return_value = {
            "worker_role": "Reader",
            "target_paths": ["app/config.py"],
        }

        from cli import command_run_task
        args = Namespace(
            workspace=Path("/tmp/ws"),
            backend="claude",
            timeout_seconds=60,
            allow_dirty_target=False,
            model=None,
        )
        with pytest.raises(CliError, match="SLICE_COMPLETENESS_VIOLATION"):
            command_run_task(args)

        # Verify ledger recorded failure
        mocks["ledger"].record_worker_execution_failure.assert_called_once()
        kwargs = mocks["ledger"].record_worker_execution_failure.call_args[1]
        assert kwargs["failure_stage"] == "precondition_check"
        assert "INFRASTRUCTURE_DEFECT" in kwargs["error_message"]

    @patch("cli.process_candidate_events")
    @patch("cli.enrich_candidate_events")
    @patch("cli.load_json")
    @patch("cli.select_and_create_adapter")
    @patch("cli.MemorySliceBuilder")
    @patch("cli.RunLedger")
    @patch("cli.TaskQueueStore")
    @patch("cli.bind_task_snapshot")
    @patch("cli.load_workspace_config")
    @patch("cli.TaskPlanner")
    @patch("cli.select_task_for_execution")
    def test_no_false_positive_with_populated_target_sources(
        self, mock_select_task, mock_planner_cls, mock_config,
        mock_bind_snap, mock_queue_cls, mock_ledger_cls,
        mock_builder_cls, mock_select_adapter, mock_load_json,
        mock_enrich, mock_process,
    ):
        """When target_sources is present, precondition should NOT fire."""
        from argparse import Namespace

        mocks = _build_cli_mocks({}, has_target_sources=True)
        mock_task = mocks["task"]

        mock_config.return_value = {
            "audit_id": "audit_test",
            "target_repo_path": str(Path("/tmp/fake")),
            "policy": "low_noise",
        }
        mock_bind_snap.return_value = mocks["snapshot"]
        mock_queue_cls.return_value = mocks["queue"]
        mock_ledger_cls.return_value = mocks["ledger"]
        mock_builder_cls.return_value = mocks["builder"]
        mock_select_adapter.return_value = (MagicMock(value="claude_sdk"), mocks["adapter"])
        mock_select_task.return_value = (mock_task, "fresh")

        # Slice WITH target_sources
        mock_load_json.return_value = {
            "worker_role": "Reader",
            "target_paths": ["app/config.py"],
            "target_sources": [
                {"file_path": "app/config.py", "snapshot_ref": "snap_abc", "file_content": "x = 1"}
            ],
        }
        mock_enrich.return_value = []
        mock_proc_result = MagicMock()
        mock_proc_result.accepted_events = 0
        mock_proc_result.rejected_events = 0
        mock_proc_result.event_outcomes = []
        mock_proc_result.trace_entry_id = "trace_001"
        mock_process.return_value = mock_proc_result

        from cli import command_run_task
        args = Namespace(
            workspace=Path("/tmp/ws"),
            backend="claude",
            timeout_seconds=60,
            allow_dirty_target=False,
            model=None,
        )
        # Should NOT raise SLICE_COMPLETENESS_VIOLATION
        # It raises "no accepted candidate events" instead (different path)
        with pytest.raises(CliError, match="no accepted candidate"):
            command_run_task(args)

        # Backend WAS called (precondition didn't block it)
        mocks["adapter"].run_with_result.assert_called_once()
        # Ledger should NOT have recorded precondition failure
        mocks["ledger"].record_worker_execution_failure.assert_not_called()
