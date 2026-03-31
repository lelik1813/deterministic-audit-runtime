"""Test: Directory target in slice_builder.

Regression test for SLICE_COMPLETENESS_VIOLATION when target is a directory
(e.g. "." or "app"). The slice builder should expand directory targets
into individual file sources using git ls-tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from runtime.policies import AuditPolicy
from runtime.slice_builder import MemorySliceBuilder
from runtime.tasks import AuditTask, TaskTarget


def _make_task(target_value: str = "app") -> AuditTask:
    return AuditTask.create(
        audit_id="audit_test",
        task_type="module_scan",
        target=TaskTarget(
            kind="path",
            value=target_value,
            snapshot_ref="abc123",
        ),
    )


def _make_state() -> dict:
    return {
        "audit": {"id": "audit_test", "status": "initialized"},
        "observations": {},
        "hypotheses": {},
        "questions": {},
        "issues": {},
        "contradictions": {},
        "decisions": {},
        "candidates": {},
        "tasks": {},
    }


def _make_policy() -> AuditPolicy:
    """Create a low_noise-equivalent policy without loading from file."""
    from runtime.policies import (
        CandidateRoutingPolicy,
        ComposeIssueBudgetPolicy,
        IssueCompositionPolicy,
        QuestionEmissionPolicy,
        TaskExpansionPolicy,
        VerifyClaimBudgetPolicy,
    )

    return AuditPolicy(
        profile_name="low_noise",
        description="Test policy",
        question_emission=QuestionEmissionPolicy(
            emit_on_ambiguity=False,
            emit_on_security_concern=True,
            emit_on_unclear_intent=False,
            suppress_near_duplicates=True,
            max_questions_per_slice=4,
        ),
        task_expansion=TaskExpansionPolicy(
            verify_claim_per_observation=True,
            compose_issue_for_verified=True,
            defer_on_budget_soft=True,
        ),
        verify_claim_budget=VerifyClaimBudgetPolicy(
            max_per_audit=100,
            max_per_observation=2,
            max_follow_up_per_iteration=50,
        ),
        compose_issue_budget=ComposeIssueBudgetPolicy(
            max_per_audit=50,
            max_per_source_path=3,
            allow_inferred_evidence=False,
        ),
        issue_composition=IssueCompositionPolicy(
            require_rule_binding_for_severity=True,
            include_low_confidence=False,
            min_evidence_class="derived_structural_fact",
        ),
        candidate_routing=CandidateRoutingPolicy(
            route_risk_candidates=True,
            route_policy_candidates=True,
            route_cross_file_correlations=True,
            route_verification_targets=True,
            max_candidates_per_audit=100,
            max_verify_tasks_per_run=20,
            max_total_tasks_per_candidate=3,
            max_module_scan_per_cross_file=3,
            defer_low_confidence=True,
            suppress_near_duplicate_files=True,
        ),
    )


def _setup_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with config and state."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "state").mkdir()
    (ws / "state" / "slices").mkdir()
    (ws / "audit_config.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "audit_id": "audit_test",
        "policy": "low_noise",
    }))
    (ws / "state" / "canonical_state.json").write_text(json.dumps(_make_state()))
    return ws


class TestDirectoryTargetExpansion:
    def test_directory_target_expands_to_files(self, tmp_path):
        """When target is a directory, slice builder should list files via git ls-tree."""
        ws = _setup_workspace(tmp_path)
        task = _make_task("app")
        policy = _make_policy()

        snapshot = MagicMock()
        snapshot.snapshot_ref = "abc123"

        def mock_read_text(path):
            if path.startswith("app/") and path.endswith(".py"):
                return f"# content of {path}"
            raise Exception(f"Cannot read: {path}")

        snapshot.read_text = mock_read_text
        snapshot.compute_file_hash = MagicMock(return_value="hash123")

        builder = MemorySliceBuilder(ws, policy=policy)

        with patch.object(
            MemorySliceBuilder,
            "_list_directory_files",
            return_value=["app/__init__.py", "app/main.py", "app/routes.py"],
        ):
            slice_result = builder.build_slice(
                task.id,
                canonical_state=_make_state(),
                task=task,
                snapshot=snapshot,
            )

        sources = slice_result.get("target_sources", [])
        assert len(sources) == 3, f"Expected 3 sources, got {len(sources)}: {sources}"
        file_paths = {s["file_path"] for s in sources}
        assert "app/__init__.py" in file_paths
        assert "app/main.py" in file_paths
        assert "app/routes.py" in file_paths

    def test_file_target_works_as_before(self, tmp_path):
        """File targets should continue to work without expansion."""
        ws = _setup_workspace(tmp_path)
        task = _make_task("app/main.py")
        policy = _make_policy()

        snapshot = MagicMock()
        snapshot.snapshot_ref = "abc123"
        snapshot.read_text = MagicMock(return_value="# main content")
        snapshot.compute_file_hash = MagicMock(return_value="hash456")

        builder = MemorySliceBuilder(ws, policy=policy)
        slice_result = builder.build_slice(
            task.id,
            canonical_state=_make_state(),
            task=task,
            snapshot=snapshot,
        )

        sources = slice_result.get("target_sources", [])
        assert len(sources) == 1
        assert sources[0]["file_path"] == "app/main.py"

    def test_empty_directory_returns_empty_sources(self, tmp_path):
        """If git ls-tree returns nothing, target_sources should be empty."""
        ws = _setup_workspace(tmp_path)
        task = _make_task("empty_dir")
        policy = _make_policy()

        snapshot = MagicMock()
        snapshot.snapshot_ref = "abc123"
        snapshot.read_text = MagicMock(side_effect=Exception("Not found"))

        builder = MemorySliceBuilder(ws, policy=policy)

        with patch.object(
            MemorySliceBuilder,
            "_list_directory_files",
            return_value=[],
        ):
            slice_result = builder.build_slice(
                task.id,
                canonical_state=_make_state(),
                task=task,
                snapshot=snapshot,
            )

        sources = slice_result.get("target_sources", [])
        assert sources == []


class TestListDirectoryFiles:
    def test_returns_file_paths_from_git_output(self, tmp_path):
        """_list_directory_files should parse git ls-tree output."""
        snapshot = MagicMock()
        snapshot.snapshot_ref = "abc123"
        snapshot.repo_root = tmp_path

        ls_output = "app/__init__.py\napp/main.py\napp/config.py\n"

        with patch("runtime.snapshot.RepositorySnapshot._git", return_value=ls_output):
            result = MemorySliceBuilder._list_directory_files(snapshot, "app")

        assert result == ["app/__init__.py", "app/main.py", "app/config.py"]

    def test_skips_trailing_slash_entries(self, tmp_path):
        """Should skip directory entries (ending with /)."""
        snapshot = MagicMock()
        snapshot.snapshot_ref = "abc123"
        snapshot.repo_root = tmp_path

        ls_output = "app/subdir/\napp/file.py\n"

        with patch("runtime.snapshot.RepositorySnapshot._git", return_value=ls_output):
            result = MemorySliceBuilder._list_directory_files(snapshot, "app")

        assert result == ["app/file.py"]

    def test_git_failure_returns_empty(self, tmp_path):
        """If git ls-tree fails, should return empty list."""
        snapshot = MagicMock()
        snapshot.snapshot_ref = "abc123"
        snapshot.repo_root = tmp_path

        with patch("runtime.snapshot.RepositorySnapshot._git", side_effect=Exception("git failed")):
            result = MemorySliceBuilder._list_directory_files(snapshot, "app")

        assert result == []
