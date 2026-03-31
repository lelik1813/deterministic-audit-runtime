from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.report_compiler import ReportCompiler


def _base_state() -> dict:
    return {
        "audit": {
            "id": "audit_mapper_test",
            "status": "accepted",
            "title": "Mapper test",
            "target": {"path": "/tmp/repo"},
            "current_snapshot_ref": "abc123",
        },
        "observations": {},
        "issues": {},
        "questions": {},
        "contradictions": {},
        "decisions": {},
        "candidates": {},
    }


def test_observation_mapper_creates_finding_when_issues_empty(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _base_state()
    state["observations"]["obs_001"] = {
        "id": "obs_001",
        "audit_id": "audit_mapper_test",
        "status": "verified",
        "statement": "JWT decode disables verify_signature and accepts forged token.",
        "evidence_class": "direct_code_fact",
        "evidence_origin": "deterministic_pattern",
        "pattern_match_ids": ["pm_jwt_no_verify"],
        "provenance": {"source_refs": [{"file_path": "app.py"}]},
    }

    report = compiler.build_report(canonical_state=state)
    findings = report["findings"]
    suppressions = report["suppression_records"]

    assert len(findings) == 1
    assert suppressions == []
    finding = findings[0]
    assert finding["finding_id"] == "finding_obs_obs_001"
    assert finding["source_observation_ids"] == ["obs_001"]
    assert finding["issue_id"] == "derived:obs_001"
    assert finding["severity"] == "high"
    assert finding["confidence"] == "high"


def test_observation_mapper_does_not_duplicate_issue_backed_finding(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _base_state()
    state["observations"]["obs_001"] = {
        "id": "obs_001",
        "audit_id": "audit_mapper_test",
        "status": "verified",
        "statement": "CORS wildcard origin allows arbitrary domain access.",
        "evidence_class": "direct_code_fact",
        "evidence_origin": "deterministic_pattern",
        "pattern_match_ids": ["pm_cors_wildcard"],
        "provenance": {"source_refs": [{"file_path": "app.py"}]},
    }
    state["issues"]["issue_001"] = {
        "id": "issue_001",
        "audit_id": "audit_mapper_test",
        "status": "accepted",
        "title": "Wildcard CORS",
        "summary": "CORS allows all origins.",
        "severity": "high",
        "severity_rule_ref": "rule.cors.001",
        "evidence": {"observation_ids": ["obs_001"]},
    }

    report = compiler.build_report(canonical_state=state)
    findings = report["findings"]
    suppressions = report["suppression_records"]

    assert len(findings) == 1
    assert findings[0]["issue_id"] == "issue_001"
    assert findings[0]["source_observation_ids"] == ["obs_001"]
    assert suppressions == []


def test_observation_mapper_ignores_disallowed_evidence_class(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _base_state()
    state["observations"]["obs_hyp"] = {
        "id": "obs_hyp",
        "audit_id": "audit_mapper_test",
        "status": "verified",
        "statement": "Inferred hypothesis observation.",
        "evidence_class": "inferred_hypothesis",
        "provenance": {"source_refs": [{"file_path": "app.py"}]},
    }

    report = compiler.build_report(canonical_state=state)
    assert report["findings"] == []
    assert len(report["suppression_records"]) == 1
    suppression = report["suppression_records"][0]
    assert suppression["observation_id"] == "obs_hyp"
    assert suppression["reason_code"] == "policy_suppressed"


def test_observation_mapper_records_duplicate_suppression_on_id_collision(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _base_state()
    state["observations"]["obs_001"] = {
        "id": "obs_001",
        "audit_id": "audit_mapper_test",
        "status": "verified",
        "statement": "Potential weakness.",
        "evidence_class": "direct_code_fact",
        "provenance": {"source_refs": [{"file_path": "app.py"}]},
    }
    # Force collision with synthetic finding_id while not covering observation in evidence
    state["issues"]["issue_collision"] = {
        "id": "issue_collision",
        "finding_id": "finding_obs_obs_001",
        "audit_id": "audit_mapper_test",
        "status": "accepted",
        "title": "Unrelated issue",
        "summary": "Unrelated summary",
        "severity": "medium",
        "severity_rule_ref": "rule.misc.001",
        "evidence": {"observation_ids": []},
    }

    # Existing compiler requires at least one observation in issue evidence.
    # Use a second observation to keep issue valid while preserving collision for obs_001.
    state["observations"]["obs_002"] = {
        "id": "obs_002",
        "audit_id": "audit_mapper_test",
        "status": "verified",
        "statement": "Another fact.",
        "evidence_class": "direct_code_fact",
        "provenance": {"source_refs": [{"file_path": "app.py"}]},
    }
    state["issues"]["issue_collision"]["evidence"]["observation_ids"] = ["obs_002"]

    report = compiler.build_report(canonical_state=state)
    suppressions = report["suppression_records"]
    assert any(
        s["observation_id"] == "obs_001" and s["reason_code"] == "duplicate_of"
        for s in suppressions
    )
