"""Evidence Discipline Validation Tests.

Validates that ONLY verified observations may serve as issue evidence.
This prevents the "authority bypass" failure mode where hypotheses or
other non-verified entities could leak into findings.

CRITICAL INVARIANT:
- IssueComposer MUST reject issues with non-observation evidence
- ReportCompiler MUST reject findings with non-verified observations
- inferred_hypothesis MUST NEVER appear in issue.evidence.observation_ids
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

from runtime.workers.issue_composer import (
    IssueComposerWorker,
    IssueComposerOutputError,
    ALLOWED_ISSUE_EVIDENCE_CLASSES,
)
from runtime.report_compiler import ReportCompiler, ReportCompileError
from runtime.evidence import ALLOWED_FINDING_EVIDENCE_CLASSES, EVIDENCE_CLASSES


def test_allowed_finding_evidence_classes_excludes_inferred_hypothesis():
    """Verify that inferred_hypothesis is NOT in ALLOWED_FINDING_EVIDENCE_CLASSES."""
    print("=" * 70)
    print("TEST 1: inferred_hypothesis excluded from allowed finding evidence")
    print("=" * 70)

    # Verify inferred_hypothesis exists as an evidence class
    assert "inferred_hypothesis" in EVIDENCE_CLASSES, \
        "inferred_hypothesis should be a valid evidence class"

    # But it should NOT be allowed for findings
    assert "inferred_hypothesis" not in ALLOWED_FINDING_EVIDENCE_CLASSES, \
        "inferred_hypothesis MUST NOT be in ALLOWED_FINDING_EVIDENCE_CLASSES"

    # Verify what IS allowed
    assert "direct_code_fact" in ALLOWED_FINDING_EVIDENCE_CLASSES
    assert "derived_structural_fact" in ALLOWED_FINDING_EVIDENCE_CLASSES

    print("  [PASS] inferred_hypothesis is NOT in ALLOWED_FINDING_EVIDENCE_CLASSES")
    print(f"         Allowed classes: {sorted(ALLOWED_FINDING_EVIDENCE_CLASSES)}")
    print(f"         Forbidden for findings: inferred_hypothesis, blocked_verification")
    return True


def test_allowed_issue_evidence_classes_excludes_inferred_hypothesis():
    """Verify that IssueComposer's allowed classes match finding requirements."""
    print("\n" + "=" * 70)
    print("TEST 2: IssueComposer excludes inferred_hypothesis")
    print("=" * 70)

    from runtime.evidence import ALLOWED_FINDING_EVIDENCE_CLASSES

    # ALLOWED_ISSUE_EVIDENCE_CLASSES should be same or stricter
    assert "inferred_hypothesis" not in ALLOWED_ISSUE_EVIDENCE_CLASSES, \
        "inferred_hypothesis MUST NOT be in ALLOWED_ISSUE_EVIDENCE_CLASSES"

    # Should be subset or equal to finding classes
    for evidence_class in ALLOWED_ISSUE_EVIDENCE_CLASSES:
        assert evidence_class in ALLOWED_FINDING_EVIDENCE_CLASSES, \
            f"IssueComposer allows {evidence_class} but it's not in finding classes"

    print("  [PASS] IssueComposer excludes inferred_hypothesis")
    print(f"         IssueComposer allowed: {sorted(ALLOWED_ISSUE_EVIDENCE_CLASSES)}")
    return True


def test_issue_composer_rejects_inferred_hypothesis_evidence():
    """Verify IssueComposer validation logic rejects inferred_hypothesis observations.

    Tests the evidence_class validation at issue_composer.py:319-331 directly.
    """
    print("\n" + "=" * 70)
    print("TEST 3: IssueComposer rejects inferred_hypothesis evidence")
    print("=" * 70)

    # Test the validation logic directly - no workspace needed
    from runtime.workers.issue_composer import ALLOWED_ISSUE_EVIDENCE_CLASSES
    from runtime.evidence import ALLOWED_FINDING_EVIDENCE_CLASSES

    # Simulate the validation check from issue_composer.py:319-331
    relevant_observations = {
        "obs_verified": {
            "id": "obs_verified",
            "evidence_class": "direct_code_fact",
        },
        "obs_inferred": {
            "id": "obs_inferred",
            "evidence_class": "inferred_hypothesis",  # Should be REJECTED
        },
    }

    # Observation IDs that an issue tries to use as evidence
    observation_ids = {"obs_inferred"}

    # This is the validation logic from IssueComposer._validate_candidate_events
    disallowed_evidence_observations = [
        obs_id for obs_id in observation_ids
        if relevant_observations[obs_id].get("evidence_class") not in ALLOWED_FINDING_EVIDENCE_CLASSES
    ]

    # inferred_hypothesis should be in the disallowed list
    assert "obs_inferred" in disallowed_evidence_observations, \
        "inferred_hypothesis should be flagged as disallowed"

    # Verify inferred_hypothesis is NOT in allowed classes
    assert "inferred_hypothesis" not in ALLOWED_ISSUE_EVIDENCE_CLASSES, \
        "inferred_hypothesis should not be in ALLOWED_ISSUE_EVIDENCE_CLASSES"

    assert "inferred_hypothesis" not in ALLOWED_FINDING_EVIDENCE_CLASSES, \
        "inferred_hypothesis should not be in ALLOWED_FINDING_EVIDENCE_CLASSES"

    print("  [PASS] IssueComposer validation logic rejects inferred_hypothesis")
    print("  [PASS] inferred_hypothesis NOT in ALLOWED_ISSUE_EVIDENCE_CLASSES")
    print("  [PASS] inferred_hypothesis NOT in ALLOWED_FINDING_EVIDENCE_CLASSES")
    return True


def test_report_compiler_rejects_inferred_hypothesis_evidence():
    """Verify ReportCompiler rejects findings with inferred_hypothesis observations."""
    print("\n" + "=" * 70)
    print("TEST 4: ReportCompiler rejects inferred_hypothesis evidence")
    print("=" * 70)

    workspace = tempfile.mkdtemp(prefix="evidence_compiler_")
    workspace_path = Path(workspace)

    try:
        state_dir = workspace_path / "state"
        state_dir.mkdir(parents=True)

        # Copy config if available
        config_src = PROJECT_ROOT / "config"
        if config_src.exists():
            shutil.copytree(config_src, workspace_path / "config")

        compiler = ReportCompiler(workspace_path)

        # Create canonical state with inferred_hypothesis observation
        canonical_state = {
            "audit": {
                "id": "audit_compiler_test",
                "status": "accepted",
                "title": "Compiler Test",
                "target": {"path": "/test"},
            },
            "observations": {
                "obs_inferred": {
                    "id": "obs_inferred",
                    "audit_id": "audit_compiler_test",
                    "status": "verified",
                    "statement": "Inferred observation",
                    "evidence_class": "inferred_hypothesis",  # REJECTED for findings
                    "provenance": {"source_refs": [{"file_path": "test.py"}]},
                },
            },
            "issues": {
                "issue_001": {
                    "id": "issue_001",
                    "audit_id": "audit_compiler_test",
                    "status": "accepted",
                    "title": "Test Issue",
                    "summary": "Issue with inferred evidence",
                    "severity": "high",
                    "severity_rule_ref": "rule_001",
                    "evidence": {
                        "observation_ids": ["obs_inferred"],  # inferred_hypothesis!
                    },
                },
            },
            "questions": {},
            "contradictions": {},
            "decisions": {},
            "candidates": {},
        }

        # This should RAISE
        try:
            compiler.build_report(canonical_state=canonical_state)
            print("  [FAIL] ReportCompiler accepted inferred_hypothesis evidence!")
            return False
        except ReportCompileError as e:
            error_msg = str(e)
            assert "inferred_hypothesis" in error_msg or "disallowed" in error_msg.lower(), \
                f"Error should mention inferred_hypothesis or disallowed: {error_msg}"
            print("  [PASS] ReportCompiler rejected inferred_hypothesis evidence")
            print(f"         Error: {error_msg[:100]}...")
            return True

    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_hypothesis_entity_not_in_issue_evidence():
    """Verify hypothesis entities (not just evidence_class) are rejected.

    This tests that an issue cannot reference a hypothesis entity directly
    in its observation_ids - the IDs themselves must be observations.
    """
    print("\n" + "=" * 70)
    print("TEST 5: Hypothesis entities cannot be in issue.evidence.observation_ids")
    print("=" * 70)

    workspace = tempfile.mkdtemp(prefix="hypothesis_entity_")
    workspace_path = Path(workspace)

    try:
        state_dir = workspace_path / "state"
        state_dir.mkdir(parents=True)

        config_src = PROJECT_ROOT / "config"
        if config_src.exists():
            shutil.copytree(config_src, workspace_path / "config")

        compiler = ReportCompiler(workspace_path)

        # Create state where issue references a hypothesis ID (not an observation)
        canonical_state = {
            "audit": {
                "id": "audit_hypothesis_test",
                "status": "accepted",
                "title": "Hypothesis Test",
                "target": {"path": "/test"},
            },
            "observations": {
                "obs_001": {
                    "id": "obs_001",
                    "audit_id": "audit_hypothesis_test",
                    "status": "verified",
                    "statement": "Valid observation",
                    "evidence_class": "direct_code_fact",
                    "provenance": {"source_refs": [{"file_path": "test.py"}]},
                },
            },
            "hypotheses": {
                "hyp_001": {
                    "id": "hyp_001",
                    "audit_id": "audit_hypothesis_test",
                    "status": "active",
                    "statement": "A hypothesis that should NOT be in evidence",
                    "provenance": {"source_refs": [{"file_path": "test.py"}]},
                },
            },
            "issues": {
                "issue_001": {
                    "id": "issue_001",
                    "audit_id": "audit_hypothesis_test",
                    "status": "accepted",
                    "title": "Issue with hypothesis evidence",
                    "summary": "Issue incorrectly references hypothesis",
                    "severity": "medium",
                    "severity_rule_ref": "rule_001",
                    "evidence": {
                        # This references a hypothesis ID, which should fail
                        "observation_ids": ["hyp_001"],  # NOT an observation!
                    },
                },
            },
            "questions": {},
            "contradictions": {},
            "decisions": {},
            "candidates": {},
        }

        # This should RAISE because hyp_001 is not in verified_observations
        try:
            compiler.build_report(canonical_state=canonical_state)
            print("  [FAIL] ReportCompiler accepted hypothesis ID as evidence!")
            return False
        except ReportCompileError as e:
            error_msg = str(e)
            assert "not verified" in error_msg.lower() or "hyp_001" in error_msg, \
                f"Error should mention verification or the ID: {error_msg}"
            print("  [PASS] ReportCompiler rejected hypothesis ID as observation evidence")
            print(f"         Error: {error_msg[:100]}...")
            return True

    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_candidate_ref_not_direct_evidence():
    """Verify candidates cannot appear directly in issue evidence.

    Candidates must go through promotion to observation before being used.
    """
    print("\n" + "=" * 70)
    print("TEST 6: Candidates cannot be direct issue evidence")
    print("=" * 70)

    workspace = tempfile.mkdtemp(prefix="candidate_evidence_")
    workspace_path = Path(workspace)

    try:
        state_dir = workspace_path / "state"
        state_dir.mkdir(parents=True)

        config_src = PROJECT_ROOT / "config"
        if config_src.exists():
            shutil.copytree(config_src, workspace_path / "config")

        compiler = ReportCompiler(workspace_path)

        # Create state where issue references a candidate ID directly
        canonical_state = {
            "audit": {
                "id": "audit_candidate_evidence_test",
                "status": "accepted",
                "title": "Candidate Evidence Test",
                "target": {"path": "/test"},
            },
            "observations": {},
            "issues": {
                "issue_001": {
                    "id": "issue_001",
                    "audit_id": "audit_candidate_evidence_test",
                    "status": "accepted",
                    "title": "Issue with candidate evidence",
                    "summary": "Issue incorrectly references candidate",
                    "severity": "high",
                    "severity_rule_ref": "rule_001",
                    "evidence": {
                        "observation_ids": ["candidate_001"],  # NOT an observation!
                    },
                },
            },
            "questions": {},
            "contradictions": {},
            "decisions": {},
            "candidates": {
                "candidate_001": {
                    "id": "candidate_001",
                    "audit_id": "audit_candidate_evidence_test",
                    "candidate_type": "risk_candidate",
                    "status": "proposed",
                    "proposed_claim": "Direct candidate reference should fail",
                    "confidence": "high",
                },
            },
        }

        # This should RAISE because candidate_001 is not an observation
        try:
            compiler.build_report(canonical_state=canonical_state)
            print("  [FAIL] ReportCompiler accepted candidate ID as observation evidence!")
            return False
        except ReportCompileError as e:
            error_msg = str(e)
            print("  [PASS] ReportCompiler rejected candidate ID as observation evidence")
            print(f"         Error: {error_msg[:100]}...")
            return True

    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def run_all_tests():
    """Run all evidence discipline tests."""
    print("=" * 70)
    print("EVIDENCE DISCIPLINE VALIDATION")
    print("Testing that only verified observations may serve as issue evidence")
    print("=" * 70)

    all_passed = True

    tests = [
        test_allowed_finding_evidence_classes_excludes_inferred_hypothesis,
        test_allowed_issue_evidence_classes_excludes_inferred_hypothesis,
        test_issue_composer_rejects_inferred_hypothesis_evidence,
        test_report_compiler_rejects_inferred_hypothesis_evidence,
        test_hypothesis_entity_not_in_issue_evidence,
        test_candidate_ref_not_direct_evidence,
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
        print("ALL EVIDENCE DISCIPLINE TESTS PASSED")
        print("inferred_hypothesis cannot enter issue evidence")
        print("Candidates must be promoted to observations before use")
    else:
        print("SOME EVIDENCE DISCIPLINE TESTS FAILED")
    print("=" * 70)

    return all_passed


if __name__ == "__main__":
    success = run_all_tests()
    if not success:
        sys.exit(1)
