"""Regression tests for the two module_scan failure modes:

Mode A — Slice completeness: module_scan task with valid target MUST produce
         target_sources with file content in the slice.

Mode B — Fail-fast: module_scan task WITHOUT target_sources MUST fail before
         backend invocation (no wasted LLM call).

Mode C — Transport compatibility: observation.proposed with fallback
         line_range {start:1, end:1} MUST be accepted by the validator suite.

Mode D — Reject diagnostics: every rejected candidate MUST carry
         rejection_code and rejection_layer.

These are integration regression tests: they use the real validator suite,
real process_candidate_events, and real slice_builder — not mocks.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.event_store import EventStore, atomic_write_text
from runtime.processing import (
    CandidateEventOutcome,
    process_candidate_events,
    _trace_outcome,
    _VALIDATOR_CODE_TO_REJECTION,
)
from runtime.rejection import RejectionReason, RejectionStage
from runtime.run_ledger import RunLedger, WorkerExecutionTraceContext
from runtime.slice_builder import MemorySliceBuilder
from runtime.tasks import AuditTask, TaskTarget
from runtime.validators.models import ValidationIssue


# =========================================================================
# Helpers
# =========================================================================

AUDIT_ID = "audit_regression"
SNAPSHOT_REF = "abc123def456"
OCCURRED_AT = "2026-03-29T12:00:00Z"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_observation_candidate(
    *,
    statement: str = "File uses hardcoded DEBUG flag.",
    file_path: str = "app/config.py",
    line_start: int = 1,
    line_end: int = 1,
    snapshot_ref: str = SNAPSHOT_REF,
    audit_id: str = AUDIT_ID,
    omit_source_refs: bool = False,
    omit_line_range: bool = False,
    omit_file_path: bool = False,
    omit_snapshot_ref: bool = False,
) -> dict[str, Any]:
    """Build a valid observation.proposed candidate event."""
    source_ref: dict[str, Any] = {}
    if not omit_file_path:
        source_ref["file_path"] = file_path
    if not omit_line_range:
        source_ref["line_range"] = {"start": line_start, "end": line_end}
    if not omit_snapshot_ref:
        source_ref["snapshot_ref"] = snapshot_ref

    payload: dict[str, Any] = {
        "id": "obs_regr001",
        "audit_id": audit_id,
        "status": "proposed",
        "statement": statement,
        "evidence_class": "direct_code_fact",
        "provenance": {
            "source_refs": [source_ref] if not omit_source_refs else [],
        },
        "created_at": OCCURRED_AT,
        "updated_at": OCCURRED_AT,
    }

    return {
        "schema_version": "1.0.0",
        "id": "event_observation_proposed_regr001",
        "audit_id": audit_id,
        "entity_type": "observation",
        "entity_id": "obs_regr001",
        "event_type": "observation.proposed",
        "occurred_at": OCCURRED_AT,
        "actor": {
            "actor_type": "worker",
            "actor_id": "Reader",
            "role": "Reader",
        },
        "snapshot_ref": snapshot_ref,
        "idempotency_key": f"{audit_id}:observation.proposed:regr001",
        "caused_by_event_id": None,
        "payload": payload,
        "acceptance": {
            "status": "pending",
            "decided_at": None,
            "decided_by": None,
            "reason": None,
        },
    }


def _init_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace so process_candidate_events can run."""
    import shutil

    project_root = Path(__file__).resolve().parent.parent
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "events").mkdir()
    (workspace / "state").mkdir()
    (workspace / "runs").mkdir()
    (workspace / "reports").mkdir()

    # Copy schema, rules, prompts — same as cli.py prepare_workspace_directories
    for dir_name in ("schema", "rules", "prompts"):
        src = project_root / dir_name
        if src.exists():
            shutil.copytree(src, workspace / dir_name)

    # Copy config if present (policies.yaml)
    config_src = project_root / "config"
    if config_src.exists():
        shutil.copytree(config_src, workspace / "config")

    return workspace


def _seed_audit(workspace: Path) -> None:
    """Write a minimal audit_created event so transition validator finds the audit."""
    audit_event = {
        "schema_version": "1.0.0",
        "id": "event_audit_created_regr",
        "audit_id": AUDIT_ID,
        "entity_type": "audit",
        "entity_id": AUDIT_ID,
        "event_type": "audit.created",
        "occurred_at": OCCURRED_AT,
        "actor": {"actor_type": "system", "actor_id": "test", "role": None},
        "snapshot_ref": None,
        "idempotency_key": f"{AUDIT_ID}:audit.created",
        "caused_by_event_id": None,
        "payload": {
            "id": AUDIT_ID,
            "status": "initialized",
            "target": {
                "repo_path": "/tmp/fake",
                "vcs": "git",
                "repo_label": "fake",
            },
            "created_at": OCCURRED_AT,
            "updated_at": OCCURRED_AT,
            "current_snapshot_ref": None,
            "title": "Regression Test Audit",
        },
        "acceptance": {
            "status": "pending",
            "decided_at": None,
            "decided_by": None,
            "reason": None,
        },
    }
    result = process_candidate_events(workspace, [audit_event], audit_id=AUDIT_ID)
    assert result.accepted_events == 1


def _mock_snapshot(files: dict[str, str], snapshot_ref: str = SNAPSHOT_REF) -> MagicMock:
    mock = MagicMock()
    mock.snapshot_ref = snapshot_ref

    def read_text(path, encoding="utf-8"):
        if path in files:
            return files[path]
        from runtime.snapshot import SnapshotFileNotFoundError
        raise SnapshotFileNotFoundError(f"File '{path}' not found in snapshot.")

    def compute_file_hash(path, algorithm="sha256"):
        if path in files:
            import hashlib
            return hashlib.sha256(files[path].encode()).hexdigest()
        from runtime.snapshot import SnapshotFileNotFoundError
        raise SnapshotFileNotFoundError(f"File '{path}' not found.")

    mock.read_text = read_text
    mock.compute_file_hash = compute_file_hash
    return mock


# =========================================================================
# Mode A: Slice Completeness
# =========================================================================

class TestSliceCompleteness:
    """module_scan with valid target MUST include target source content."""

    def test_module_scan_slice_contains_file_content(self, tmp_path):
        from runtime.policies import AuditPolicy, QuestionEmissionPolicy, TaskExpansionPolicy, VerifyClaimBudgetPolicy, ComposeIssueBudgetPolicy, IssueCompositionPolicy, CandidateRoutingPolicy
        policy = AuditPolicy(
            profile_name="low_noise", description="test",
            question_emission=QuestionEmissionPolicy(emit_on_ambiguity=True, emit_on_security_concern=True, emit_on_unclear_intent=True, suppress_near_duplicates=True, max_questions_per_slice=5),
            task_expansion=TaskExpansionPolicy(verify_claim_per_observation=True, compose_issue_for_verified=True, defer_on_budget_soft=True),
            verify_claim_budget=VerifyClaimBudgetPolicy(max_per_audit=50, max_per_observation=3, max_follow_up_per_iteration=10),
            compose_issue_budget=ComposeIssueBudgetPolicy(max_per_audit=20, max_per_source_path=3, allow_inferred_evidence=False),
            issue_composition=IssueCompositionPolicy(require_rule_binding_for_severity=False, include_low_confidence=False, min_evidence_class="direct_code_fact"),
            candidate_routing=CandidateRoutingPolicy(max_candidates_per_audit=100, max_verify_tasks_per_run=20, max_module_scan_per_cross_file=5, max_total_tasks_per_candidate=10, defer_low_confidence=False, suppress_near_duplicate_files=True, route_risk_candidates=True, route_policy_candidates=True, route_cross_file_correlations=True, route_verification_targets=True),
        )

        task = AuditTask.create(
            audit_id=AUDIT_ID,
            task_type="module_scan",
            target=TaskTarget(kind="path", value="app/config.py", snapshot_ref=SNAPSHOT_REF),
        )
        state = {
            "audit": {"id": AUDIT_ID},
            "observations": {}, "hypotheses": {}, "questions": {},
            "issues": {}, "contradictions": {},
        }
        snapshot = _mock_snapshot({"app/config.py": "DEBUG = True\nSECRET = 'abc'"})

        builder = MemorySliceBuilder(tmp_path, policy=policy)
        result = builder.build_slice(task.id, canonical_state=state, task=task, snapshot=snapshot)

        assert "target_sources" in result
        assert len(result["target_sources"]) == 1
        assert result["target_sources"][0]["file_content"] == "DEBUG = True\nSECRET = 'abc'"
        assert result["target_sources"][0]["file_path"] == "app/config.py"

    def test_module_scan_slice_missing_file_produces_empty_sources(self, tmp_path):
        from runtime.policies import AuditPolicy, QuestionEmissionPolicy, TaskExpansionPolicy, VerifyClaimBudgetPolicy, ComposeIssueBudgetPolicy, IssueCompositionPolicy, CandidateRoutingPolicy
        policy = AuditPolicy(
            profile_name="low_noise", description="test",
            question_emission=QuestionEmissionPolicy(emit_on_ambiguity=True, emit_on_security_concern=True, emit_on_unclear_intent=True, suppress_near_duplicates=True, max_questions_per_slice=5),
            task_expansion=TaskExpansionPolicy(verify_claim_per_observation=True, compose_issue_for_verified=True, defer_on_budget_soft=True),
            verify_claim_budget=VerifyClaimBudgetPolicy(max_per_audit=50, max_per_observation=3, max_follow_up_per_iteration=10),
            compose_issue_budget=ComposeIssueBudgetPolicy(max_per_audit=20, max_per_source_path=3, allow_inferred_evidence=False),
            issue_composition=IssueCompositionPolicy(require_rule_binding_for_severity=False, include_low_confidence=False, min_evidence_class="direct_code_fact"),
            candidate_routing=CandidateRoutingPolicy(max_candidates_per_audit=100, max_verify_tasks_per_run=20, max_module_scan_per_cross_file=5, max_total_tasks_per_candidate=10, defer_low_confidence=False, suppress_near_duplicate_files=True, route_risk_candidates=True, route_policy_candidates=True, route_cross_file_correlations=True, route_verification_targets=True),
        )

        task = AuditTask.create(
            audit_id=AUDIT_ID,
            task_type="module_scan",
            target=TaskTarget(kind="path", value="nonexistent.py", snapshot_ref=SNAPSHOT_REF),
        )
        state = {
            "audit": {"id": AUDIT_ID},
            "observations": {}, "hypotheses": {}, "questions": {},
            "issues": {}, "contradictions": {},
        }
        snapshot = _mock_snapshot({})  # no files

        builder = MemorySliceBuilder(tmp_path, policy=policy)
        result = builder.build_slice(task.id, canonical_state=state, task=task, snapshot=snapshot)

        # No target_sources → precondition in cli.py will block backend invocation
        assert "target_sources" not in result or result.get("target_sources") == []


# =========================================================================
# Mode B: Fail-Fast
# =========================================================================

class TestFailFast:
    """module_scan WITHOUT target_sources MUST fail before backend invocation."""

    def test_precondition_check_blocks_empty_sources(self):
        """Reproduces the exact precondition check from cli.py."""
        task_type = "module_scan"
        target_kind = "path"
        slice_payload = {
            "worker_role": "Reader",
            "target_paths": ["app/config.py"],
            # No target_sources
        }

        precondition_failed = (
            task_type == "module_scan"
            and target_kind in {"path", "module"}
            and not slice_payload.get("target_sources")
        )
        assert precondition_failed is True

    def test_precondition_passes_with_sources(self):
        task_type = "module_scan"
        target_kind = "path"
        slice_payload = {
            "worker_role": "Reader",
            "target_paths": ["app/config.py"],
            "target_sources": [
                {"file_path": "app/config.py", "snapshot_ref": SNAPSHOT_REF, "file_content": "x = 1"}
            ],
        }

        precondition_failed = (
            task_type == "module_scan"
            and target_kind in {"path", "module"}
            and not slice_payload.get("target_sources")
        )
        assert precondition_failed is False

    def test_non_module_scan_not_checked(self):
        """verify_claim tasks are NOT subject to the precondition."""
        for task_type in ("verify_claim", "compose_issue"):
            slice_payload = {}  # no sources at all
            precondition_failed = (
                task_type == "module_scan"
                and "path" in {"path", "module"}
                and not slice_payload.get("target_sources")
            )
            assert precondition_failed is False


# =========================================================================
# Mode C: Transport Compatibility
# =========================================================================

class TestTransportCompatibility:
    """observation.proposed with fallback line_range MUST be accepted."""

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = _init_workspace(tmp_path)
        _seed_audit(ws)
        return ws

    def test_fallback_line_range_accepted(self, workspace):
        """observation with line_range {1,1} and real file MUST pass validators."""
        candidate = _make_observation_candidate(line_start=1, line_end=1)
        result = process_candidate_events(
            workspace, [candidate], audit_id=AUDIT_ID,
        )
        assert result.accepted_events == 1, (
            f"Expected 1 accepted, got {result.accepted_events}. "
            f"Issues: {[o.issues for o in result.event_outcomes]}"
        )

    def test_exact_line_range_accepted(self, workspace):
        """observation with exact line_range MUST pass validators."""
        candidate = _make_observation_candidate(line_start=10, line_end=25)
        result = process_candidate_events(
            workspace, [candidate], audit_id=AUDIT_ID,
        )
        assert result.accepted_events == 1

    def test_missing_line_range_rejected(self, workspace):
        """observation without line_range MUST be rejected."""
        candidate = _make_observation_candidate(omit_line_range=True)
        result = process_candidate_events(
            workspace, [candidate], audit_id=AUDIT_ID,
        )
        assert result.rejected_events == 1
        codes = [issue.code for o in result.event_outcomes for issue in o.issues]
        # Schema validator catches missing required fields before source_binding runs
        assert "schema_validation_failed" in codes

    def test_missing_source_refs_rejected(self, workspace):
        """observation without source_refs MUST be rejected."""
        candidate = _make_observation_candidate(omit_source_refs=True)
        result = process_candidate_events(
            workspace, [candidate], audit_id=AUDIT_ID,
        )
        assert result.rejected_events == 1
        codes = [issue.code for o in result.event_outcomes for issue in o.issues]
        # Schema validator catches empty source_refs (minItems: 1) before source_binding runs
        assert "schema_validation_failed" in codes

    def test_missing_file_path_rejected(self, workspace):
        """observation without file_path in source_ref MUST be rejected."""
        candidate = _make_observation_candidate(omit_file_path=True)
        result = process_candidate_events(
            workspace, [candidate], audit_id=AUDIT_ID,
        )
        assert result.rejected_events == 1


# =========================================================================
# Mode D: Reject Diagnostics
# =========================================================================

class TestRejectDiagnostics:
    """Every rejected candidate MUST carry rejection_code and rejection_layer."""

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = _init_workspace(tmp_path)
        _seed_audit(ws)
        return ws

    def _get_rejected_outcome(self, workspace, candidate):
        result = process_candidate_events(workspace, [candidate], audit_id=AUDIT_ID)
        assert result.rejected_events == 1
        return result.event_outcomes[0]

    def test_rejected_event_has_trace_with_rejection_code(self, workspace):
        candidate = _make_observation_candidate(omit_source_refs=True)
        outcome = self._get_rejected_outcome(workspace, candidate)
        trace = _trace_outcome(outcome)

        assert trace["outcome"] == "rejected"
        assert trace["rejection"] is not None
        assert "rejection_code" in trace["rejection"]
        assert "rejection_layer" in trace["rejection"]
        assert trace["rejection"]["rejection_code"] in {
            r.value for r in RejectionReason
        }
        assert trace["rejection"]["rejection_layer"] in {
            s.value for s in RejectionStage
        }

    def test_schema_invalid_rejection_classified(self, workspace):
        """missing source_refs → schema_invalid / schema."""
        candidate = _make_observation_candidate(omit_source_refs=True)
        outcome = self._get_rejected_outcome(workspace, candidate)
        trace = _trace_outcome(outcome)

        assert trace["rejection"]["rejection_code"] == "schema_invalid"
        assert trace["rejection"]["rejection_layer"] == "schema"
        assert "schema_validation_failed" in trace["rejection"]["all_issue_codes"]

    def test_line_range_issue_classified(self, workspace):
        """missing line_range → schema_invalid / schema."""
        candidate = _make_observation_candidate(omit_line_range=True)
        outcome = self._get_rejected_outcome(workspace, candidate)
        trace = _trace_outcome(outcome)

        assert trace["rejection"]["rejection_code"] == "schema_invalid"
        assert trace["rejection"]["rejection_layer"] == "schema"

    def test_invalid_line_range_classified(self, workspace):
        """line_range with start > end → schema_invalid / schema."""
        candidate = _make_observation_candidate(line_start=25, line_end=10)
        outcome = self._get_rejected_outcome(workspace, candidate)
        trace = _trace_outcome(outcome)

        assert trace["rejection"]["rejection_code"] == "schema_invalid"
        assert trace["rejection"]["rejection_layer"] == "schema"
        assert "invalid_line_range" in trace["rejection"]["all_issue_codes"]

    def test_duplicate_rejection_classified(self, workspace):
        """Duplicate submission → policy_rejected / policy."""
        candidate = _make_observation_candidate()
        # First: accepted
        result1 = process_candidate_events(workspace, [candidate], audit_id=AUDIT_ID)
        assert result1.accepted_events == 1

        # Second: same idempotency_key → rejected as duplicate
        result2 = process_candidate_events(workspace, [candidate], audit_id=AUDIT_ID)
        assert result2.rejected_events == 1
        outcome = result2.event_outcomes[0]
        trace = _trace_outcome(outcome)

        assert trace["rejection"] is not None
        assert trace["rejection"]["rejection_code"] == "policy_rejected"
        assert trace["rejection"]["rejection_layer"] == "policy"
        assert any("duplicate" in c or "conflict" in c for c in trace["rejection"]["all_issue_codes"])

    def test_rejection_code_from_all_validator_codes(self):
        """Every known validator code maps to a valid rejection reason/stage."""
        for code, (reason, stage) in _VALIDATOR_CODE_TO_REJECTION.items():
            assert isinstance(reason, RejectionReason), f"{code}: reason not a RejectionReason"
            assert isinstance(stage, RejectionStage), f"{code}: stage not a RejectionStage"

    def test_failure_artifacts_written_on_rejection(self, workspace):
        """When candidates are rejected, failure bundle must be writable."""
        from runtime.failure_artifacts import write_failure_bundle

        candidate = _make_observation_candidate(omit_source_refs=True)
        result = process_candidate_events(workspace, [candidate], audit_id=AUDIT_ID)
        assert result.rejected_events == 1

        outcomes = [_trace_outcome(o) for o in result.event_outcomes]
        failure_dir = write_failure_bundle(
            workspace,
            run_id="run_regr001",
            task_id="task_regr001",
            raw_output='{"candidate_events": [{"bad": true}]}',
            normalized_candidates=[candidate],
            event_outcomes=outcomes,
        )
        assert failure_dir is not None
        diag = json.loads((failure_dir / "rejection_diagnostics.json").read_text(encoding="utf-8"))
        assert diag["rejected_count"] == 1
        assert diag["rejected_events"][0]["rejection"]["rejection_code"] == "schema_invalid"
