from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.report_compiler import ReportCompileError, ReportCompiler


def test_compiled_report_summary_count_mismatch_rejected() -> None:
    report = {
        "summary": {
            "finding_count": 0,
            "verified_observation_count": 0,
            "open_question_count": 0,
            "contradiction_count": 0,
            "decision_count": 0,
        },
        "findings": [{"finding_id": "f1"}],
        "verified_observations": [],
        "open_questions": [],
        "contradictions": [],
        "decisions": [],
        "suppression_records": [],
    }
    with pytest.raises(ReportCompileError, match="Summary inconsistency"):
        ReportCompiler._validate_compiled_report_consistency(report)


def test_compiled_report_requires_suppression_coverage_for_eligible_without_findings() -> None:
    report = {
        "summary": {
            "finding_count": 0,
            "verified_observation_count": 1,
            "open_question_count": 0,
            "contradiction_count": 0,
            "decision_count": 0,
        },
        "findings": [],
        "verified_observations": [
            {
                "observation_id": "obs_001",
                "evidence_class": "direct_code_fact",
                "statement": "Fact",
                "status": "verified",
            }
        ],
        "open_questions": [],
        "contradictions": [],
        "decisions": [],
        "suppression_records": [],
    }
    with pytest.raises(ReportCompileError, match="missing suppression coverage"):
        ReportCompiler._validate_compiled_report_consistency(report)


def test_compiled_report_allows_suppression_coverage_for_eligible_without_findings() -> None:
    report = {
        "summary": {
            "finding_count": 0,
            "verified_observation_count": 1,
            "open_question_count": 0,
            "contradiction_count": 0,
            "decision_count": 0,
        },
        "findings": [],
        "verified_observations": [
            {
                "observation_id": "obs_001",
                "evidence_class": "direct_code_fact",
                "statement": "Fact",
                "status": "verified",
            }
        ],
        "open_questions": [],
        "contradictions": [],
        "decisions": [],
        "suppression_records": [
            {
                "suppression_id": "sup_obs_001",
                "observation_id": "obs_001",
                "reason_code": "below_threshold",
                "reason_detail": "example",
                "status": "suppressed",
            }
        ],
    }
    ReportCompiler._validate_compiled_report_consistency(report)


def test_build_report_consistency_guard_accepts_current_pipeline(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = {
        "audit": {
            "id": "audit_consistency_build",
            "status": "analyzed",
            "title": "Consistency build",
            "target": {"path": "/tmp/repo"},
            "current_snapshot_ref": "abc123",
        },
        "observations": {
            "obs_001": {
                "id": "obs_001",
                "audit_id": "audit_consistency_build",
                "status": "verified",
                "statement": "JWT verify_signature disabled.",
                "evidence_class": "direct_code_fact",
                "evidence_origin": "deterministic_pattern",
                "provenance": {"source_refs": [{"file_path": "app.py"}]},
            }
        },
        "issues": {},
        "questions": {},
        "contradictions": {},
        "decisions": {},
        "candidates": {},
    }
    report = compiler.build_report(canonical_state=state)
    assert report["summary"]["verified_observation_count"] == 1
    assert len(report["findings"]) == 1

