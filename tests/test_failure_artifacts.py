"""Tests for failure artifact persistence.

Validates:
  1. write_failure_bundle writes raw/normalized/rejection artifacts when rejections exist
  2. write_failure_bundle skips when no rejections
  3. Ledger normalizer preserves rejection field
  4. _write_json does not raise on failure
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.failure_artifacts import write_failure_bundle, _write_json


# ---------------------------------------------------------------------------
# Unit tests: write_failure_bundle
# ---------------------------------------------------------------------------

class TestWriteFailureBundle:

    def test_skips_when_no_rejections(self, tmp_path):
        result = write_failure_bundle(
            tmp_path,
            run_id="run_0001",
            task_id="task_001",
            raw_output='{"ok": true}',
            normalized_candidates=[{"id": "ev1"}],
            event_outcomes=[
                {"outcome": "accepted", "event_id": "ev1"},
            ],
        )
        assert result is None
        assert not (tmp_path / "runs" / "failures").exists()

    def test_writes_all_three_artifacts(self, tmp_path):
        outcomes = [
            {
                "outcome": "rejected",
                "event_id": "ev1",
                "event_type": "observation.proposed",
                "entity_type": "observation",
                "entity_id": "obs_001",
                "rejection": {
                    "rejection_code": "schema_invalid",
                    "rejection_layer": "schema",
                    "rejection_message": "missing field",
                    "validator": "schema",
                    "validator_code": "schema_validation_failed",
                    "all_issue_codes": ["schema_validation_failed"],
                },
                "issue_codes": ["schema_validation_failed"],
            },
        ]
        result = write_failure_bundle(
            tmp_path,
            run_id="run_0001",
            task_id="task_001",
            raw_output='{"candidate_events": [{"bad": true}]}',
            normalized_candidates=[{"bad": True}],
            event_outcomes=outcomes,
        )

        assert result is not None
        assert result.exists()

        raw = json.loads((result / "raw_output.json").read_text(encoding="utf-8"))
        assert raw["run_id"] == "run_0001"
        assert raw["raw_output"] == '{"candidate_events": [{"bad": true}]}'

        norm = json.loads((result / "normalized_candidates.json").read_text(encoding="utf-8"))
        assert norm["candidate_count"] == 1
        assert norm["candidates"] == [{"bad": True}]

        diag = json.loads((result / "rejection_diagnostics.json").read_text(encoding="utf-8"))
        assert diag["rejected_count"] == 1
        assert diag["accepted_count"] == 0
        assert len(diag["rejected_events"]) == 1
        assert diag["rejected_events"][0]["rejection"]["rejection_code"] == "schema_invalid"

    def test_handles_none_raw_output(self, tmp_path):
        outcomes = [{"outcome": "rejected", "event_id": "ev1"}]
        result = write_failure_bundle(
            tmp_path,
            run_id="run_0002",
            task_id="task_002",
            raw_output=None,
            normalized_candidates=[],
            event_outcomes=outcomes,
        )
        assert result is not None
        assert not (result / "raw_output.json").exists()
        assert (result / "normalized_candidates.json").exists()
        assert (result / "rejection_diagnostics.json").exists()

    def test_handles_mixed_accepted_rejected(self, tmp_path):
        outcomes = [
            {"outcome": "accepted", "event_id": "ev1"},
            {
                "outcome": "rejected",
                "event_id": "ev2",
                "rejection": {"rejection_code": "policy_rejected"},
            },
        ]
        result = write_failure_bundle(
            tmp_path,
            run_id="run_0003",
            task_id="task_003",
            raw_output="...",
            normalized_candidates=[{"id": "ev1"}, {"id": "ev2"}],
            event_outcomes=outcomes,
        )
        assert result is not None
        diag = json.loads((result / "rejection_diagnostics.json").read_text(encoding="utf-8"))
        assert diag["accepted_count"] == 1
        assert diag["rejected_count"] == 1

    def test_creates_failure_directory(self, tmp_path):
        outcomes = [{"outcome": "rejected", "event_id": "ev1"}]
        result = write_failure_bundle(
            tmp_path,
            run_id="run_0042",
            task_id="task_042",
            raw_output="x",
            normalized_candidates=[],
            event_outcomes=outcomes,
        )
        assert result == tmp_path / "runs" / "failures" / "run_0042"


class TestWriteJsonSafe:

    def test_write_json_normal(self, tmp_path):
        path = tmp_path / "test.json"
        _write_json(path, {"key": "value"})
        assert json.loads(path.read_text(encoding="utf-8"))["key"] == "value"

    def test_write_json_handles_non_serializable(self, tmp_path):
        path = tmp_path / "test.json"
        # Path objects are not JSON-serializable by default, but default=str handles it
        _write_json(path, {"path": Path("/some/path")})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "path" in data
        assert isinstance(data["path"], str)

    def test_write_json_silent_on_failure(self, tmp_path):
        # Writing to a directory path should not raise
        path = tmp_path  # this is a directory, not a file
        _write_json(path / "nonexistent" / "deep" / "file.json", {"key": "val"})
        # No assertion needed — just verify no exception


# ---------------------------------------------------------------------------
# Integration: ledger preserves rejection field
# ---------------------------------------------------------------------------

class TestLedgerRejectionField:

    def test_normalize_event_outcome_preserves_rejection(self, tmp_path):
        from runtime.run_ledger import RunLedger

        ledger = RunLedger(tmp_path)
        outcome_with_rejection = {
            "event_id": "ev_001",
            "event_type": "observation.proposed",
            "entity_type": "observation",
            "entity_id": "obs_001",
            "outcome": "rejected",
            "issue_codes": ["schema_validation_failed"],
            "rejection": {
                "rejection_code": "schema_invalid",
                "rejection_layer": "schema",
                "rejection_message": "Required field missing",
                "validator": "schema",
                "validator_code": "schema_validation_failed",
                "all_issue_codes": ["schema_validation_failed"],
            },
            "ledger_line_number": None,
            "ledger_path": None,
        }

        normalized = RunLedger._normalize_event_outcome(outcome_with_rejection)
        assert "rejection" in normalized
        assert normalized["rejection"]["rejection_code"] == "schema_invalid"
        assert normalized["rejection"]["rejection_layer"] == "schema"

    def test_normalize_event_outcome_no_rejection_field(self, tmp_path):
        from runtime.run_ledger import RunLedger

        outcome_without_rejection = {
            "event_id": "ev_002",
            "event_type": "observation.proposed",
            "entity_type": "observation",
            "entity_id": "obs_002",
            "outcome": "accepted",
            "issue_codes": [],
            "ledger_line_number": 5,
            "ledger_path": "/fake/ledger.json",
        }

        normalized = RunLedger._normalize_event_outcome(outcome_without_rejection)
        assert "rejection" not in normalized

    def test_rejection_persisted_in_ledger(self, tmp_path):
        from runtime.run_ledger import RunLedger, WorkerExecutionTraceContext

        ledger = RunLedger(tmp_path)
        ledger.start_run(audit_id="audit_test", snapshot_ref="snap_001")
        ctx = WorkerExecutionTraceContext(
            run_id="run_0001",
            audit_id="audit_test",
            task_id="task_001",
            slice_id="slice_001",
            worker_role="Reader",
            adapter_invocation={"backend": "codex"},
            input_digest="d1",
            output_digest="d2",
        )

        entry = ledger.record_worker_execution(
            trace_context=ctx,
            total_candidate_events=1,
            accepted_events=0,
            rejected_events=1,
            event_outcomes=[
                {
                    "event_id": "ev_001",
                    "event_type": "observation.proposed",
                    "entity_type": "observation",
                    "entity_id": "obs_001",
                    "outcome": "rejected",
                    "issue_codes": ["schema_validation_failed"],
                    "rejection": {
                        "rejection_code": "schema_invalid",
                        "rejection_layer": "schema",
                        "rejection_message": "Required field missing",
                        "validator": "schema",
                        "validator_code": "schema_validation_failed",
                        "all_issue_codes": ["schema_validation_failed"],
                    },
                    "ledger_line_number": None,
                    "ledger_path": None,
                },
            ],
        )

        # Verify rejection is in the persisted entry
        rejected = entry.get("rejected_events", [])
        assert len(rejected) == 1
        assert "rejection" in rejected[0]
        assert rejected[0]["rejection"]["rejection_code"] == "schema_invalid"

        # Verify round-trip through ledger
        entries = ledger.read_entries()
        worker_entries = [e for e in entries if e.get("entry_type") == "worker_execution"]
        assert len(worker_entries) == 1
        persisted_rejected = worker_entries[0]["rejected_events"]
        assert persisted_rejected[0]["rejection"]["rejection_code"] == "schema_invalid"
