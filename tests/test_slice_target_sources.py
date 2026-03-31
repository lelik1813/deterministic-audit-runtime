"""Tests for target_sources population in module_scan slices.

Validates the Slice Completeness Invariant:
  For module_scan tasks, when a RepositorySnapshot is provided,
  the slice MUST include target_sources with file content.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.tasks import AuditTask, TaskTarget
from runtime.slice_builder import MemorySliceBuilder, SliceBuildError
from runtime.policies import (
    AuditPolicy,
    CandidateRoutingPolicy,
    ComposeIssueBudgetPolicy,
    IssueCompositionPolicy,
    QuestionEmissionPolicy,
    TaskExpansionPolicy,
    VerifyClaimBudgetPolicy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUDIT_ID = "audit_20260101"
SNAPSHOT_REF = "abc123def456"


def _make_task(task_type: str = "module_scan", target_kind: str = "path", target_value: str = "app/config.py") -> AuditTask:
    target = TaskTarget(kind=target_kind, value=target_value, snapshot_ref=SNAPSHOT_REF)
    return AuditTask.create(audit_id=AUDIT_ID, task_type=task_type, target=target)


def _make_state() -> dict:
    return {
        "audit": {"id": AUDIT_ID},
        "observations": {},
        "hypotheses": {},
        "questions": {},
        "issues": {},
        "contradictions": {},
    }


def _mock_snapshot(files: dict[str, str], snapshot_ref: str = SNAPSHOT_REF) -> MagicMock:
    """Create a mock RepositorySnapshot that returns given file contents."""
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


def _stub_policy() -> AuditPolicy:
    return AuditPolicy(
        profile_name="low_noise",
        description="test policy",
        question_emission=QuestionEmissionPolicy(
            emit_on_ambiguity=True,
            emit_on_security_concern=True,
            emit_on_unclear_intent=True,
            suppress_near_duplicates=True,
            max_questions_per_slice=5,
        ),
        task_expansion=TaskExpansionPolicy(
            verify_claim_per_observation=True,
            compose_issue_for_verified=True,
            defer_on_budget_soft=True,
        ),
        verify_claim_budget=VerifyClaimBudgetPolicy(
            max_per_audit=50,
            max_per_observation=3,
            max_follow_up_per_iteration=10,
        ),
        compose_issue_budget=ComposeIssueBudgetPolicy(
            max_per_audit=20,
            max_per_source_path=3,
            allow_inferred_evidence=False,
        ),
        issue_composition=IssueCompositionPolicy(
            require_rule_binding_for_severity=False,
            include_low_confidence=False,
            min_evidence_class="direct_code_fact",
        ),
        candidate_routing=CandidateRoutingPolicy(
            max_candidates_per_audit=100,
            max_verify_tasks_per_run=20,
            max_module_scan_per_cross_file=5,
            max_total_tasks_per_candidate=10,
            defer_low_confidence=False,
            suppress_near_duplicate_files=True,
            route_risk_candidates=True,
            route_policy_candidates=True,
            route_cross_file_correlations=True,
            route_verification_targets=True,
        ),
    )


def _build_slice(tmp_path, task, state, snapshot=None):
    builder = MemorySliceBuilder(tmp_path, policy=_stub_policy())
    return builder.build_slice(
        task.id,
        canonical_state=state,
        task=task,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Tests: module_scan with snapshot
# ---------------------------------------------------------------------------

class TestModuleScanTargetSources:

    def test_target_sources_present_when_snapshot_provided(self, tmp_path):
        task = _make_task()
        state = _make_state()
        snapshot = _mock_snapshot({"app/config.py": "DEBUG = True\nSECRET = 'abc'"})

        result = _build_slice(tmp_path, task, state, snapshot)

        assert "target_sources" in result
        assert len(result["target_sources"]) >= 1

    def test_target_sources_contains_file_content(self, tmp_path):
        content = "DEBUG = True\nSECRET = 'abc'"
        task = _make_task()
        state = _make_state()
        snapshot = _mock_snapshot({"app/config.py": content})

        result = _build_slice(tmp_path, task, state, snapshot)

        sources = result["target_sources"]
        matching = [s for s in sources if s["file_path"] == "app/config.py"]
        assert len(matching) == 1
        assert matching[0]["file_content"] == content

    def test_target_sources_file_path_matches_target_paths(self, tmp_path):
        task = _make_task()
        state = _make_state()
        snapshot = _mock_snapshot({"app/config.py": "x = 1"})

        result = _build_slice(tmp_path, task, state, snapshot)

        source_paths = {s["file_path"] for s in result["target_sources"]}
        for tp in result["target_paths"]:
            assert tp in source_paths, f"target_path '{tp}' not in target_sources"

    def test_target_sources_snapshot_ref_matches(self, tmp_path):
        task = _make_task()
        state = _make_state()
        snapshot = _mock_snapshot({"app/config.py": "x = 1"}, snapshot_ref=SNAPSHOT_REF)

        result = _build_slice(tmp_path, task, state, snapshot)

        for source in result["target_sources"]:
            assert source["snapshot_ref"] == SNAPSHOT_REF

    def test_target_sources_includes_file_hash(self, tmp_path):
        task = _make_task()
        state = _make_state()
        snapshot = _mock_snapshot({"app/config.py": "x = 1"})

        result = _build_slice(tmp_path, task, state, snapshot)

        for source in result["target_sources"]:
            assert "file_hash" in source
            assert isinstance(source["file_hash"], str)
            assert len(source["file_hash"]) >= 7

    def test_missing_file_in_snapshot_skipped(self, tmp_path):
        task = _make_task(target_value="nonexistent.py")
        state = _make_state()
        snapshot = _mock_snapshot({})  # No files

        result = _build_slice(tmp_path, task, state, snapshot)

        # target_sources should not be present (empty list → omitted)
        assert "target_sources" not in result or result.get("target_sources") == []


# ---------------------------------------------------------------------------
# Tests: non-module_scan tasks
# ---------------------------------------------------------------------------

class TestOtherTaskTypesNoTargetSources:

    def test_verify_claim_no_target_sources(self, tmp_path):
        target = TaskTarget(kind="observation", value="obs_001", snapshot_ref=SNAPSHOT_REF)
        task = AuditTask.create(audit_id=AUDIT_ID, task_type="verify_claim", target=target)
        state = _make_state()
        state["observations"]["obs_001"] = {
            "id": "obs_001",
            "audit_id": AUDIT_ID,
            "statement": "test",
            "status": "proposed",
            "evidence_class": "direct_code_fact",
            "provenance": {"source_refs": []},
        }
        snapshot = _mock_snapshot({"app/config.py": "x = 1"})

        result = _build_slice(tmp_path, task, state, snapshot)

        assert "target_sources" not in result or result.get("target_sources") == []


# ---------------------------------------------------------------------------
# Tests: backward compatibility (no snapshot)
# ---------------------------------------------------------------------------

class TestBackwardCompatNoSnapshot:

    def test_no_target_sources_without_snapshot(self, tmp_path):
        task = _make_task()
        state = _make_state()

        result = _build_slice(tmp_path, task, state, snapshot=None)

        assert "target_sources" not in result

    def test_write_slice_without_snapshot(self, tmp_path):
        task = _make_task()
        state = _make_state()

        builder = MemorySliceBuilder(tmp_path, policy=_stub_policy())
        slice_result = builder.write_slice(
            task.id,
            canonical_state=state,
            task=task,
            snapshot=None,
        )
        assert slice_result.slice_path.exists()
        data = json.loads(slice_result.slice_path.read_text(encoding="utf-8"))
        assert "target_sources" not in data

    def test_write_slice_with_snapshot(self, tmp_path):
        task = _make_task()
        state = _make_state()
        snapshot = _mock_snapshot({"app/config.py": "DEBUG = True"})

        builder = MemorySliceBuilder(tmp_path, policy=_stub_policy())
        slice_result = builder.write_slice(
            task.id,
            canonical_state=state,
            task=task,
            snapshot=snapshot,
        )
        assert slice_result.slice_path.exists()
        data = json.loads(slice_result.slice_path.read_text(encoding="utf-8"))
        assert "target_sources" in data
        assert len(data["target_sources"]) >= 1
