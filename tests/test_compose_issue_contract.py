"""
Unit tests for Compose Issue Enforcement (STEP 5)

These tests verify that:
1. compose_issue produces either valid candidates OR explicit structured rejection
2. No silent empty output is allowed
3. Every no-candidates case has an explicit reason
4. Non-JSON is visible and classified
5. Acceptance funnel is fully transparent

Core Invariant:
Compose_issue MUST produce either valid candidates OR explicit structured rejection
"""

from __future__ import annotations

import pytest

from runtime.compose_issue_contract import (
    ComposeIssueContractViolation,
    ComposeIssueOutput,
    NoCandidatesReason,
    StructuredExplanation,
    classify_empty_candidates,
    enforce_compose_issue_contract,
    create_insufficient_evidence_explanation,
    create_no_verified_observations_explanation,
    create_open_questions_explanation,
    create_other_explanation,
    SCHEMA_VERSION,
)
from runtime.rejection import RejectionReason, RejectionStage


class TestNoCandidatesReason:
    """Tests for NoCandidatesReason enum."""

    def test_all_reasons_defined(self) -> None:
        """Verify all required reason codes exist."""
        required_reasons = {
            "insufficient_evidence",
            "evidence_class_mismatch",
            "missing_required_observation",
            "open_questions_unresolved",
            "constraint_violation",
            "no_verified_observations",
            "policy_prevents_issue",
            "other",
        }
        actual_reasons = {reason.value for reason in NoCandidatesReason}
        assert required_reasons == actual_reasons

    def test_reason_values_are_snake_case(self) -> None:
        """Reason values should be snake_case for consistency."""
        for reason in NoCandidatesReason:
            assert reason.value.islower() or reason.value == "other"
            assert " " not in reason.value


class TestStructuredExplanation:
    """Tests for StructuredExplanation dataclass."""

    def test_minimal_explanation(self) -> None:
        """Minimal explanation requires reason and summary."""
        explanation = StructuredExplanation(
            reason=NoCandidatesReason.INSUFFICIENT_EVIDENCE,
            summary="Not enough evidence to compose an issue.",
        )
        assert explanation.reason == NoCandidatesReason.INSUFFICIENT_EVIDENCE
        assert explanation.summary == "Not enough evidence to compose an issue."
        assert explanation.details == {}
        assert explanation.observation_ids_referenced == []
        assert explanation.question_ids_blocking == []

    def test_full_explanation(self) -> None:
        """Full explanation includes all optional fields."""
        explanation = StructuredExplanation(
            reason=NoCandidatesReason.OPEN_QUESTIONS_UNRESOLVED,
            summary="Critical questions remain unanswered.",
            details={"question_count": 3},
            observation_ids_referenced=["obs_1", "obs_2"],
            question_ids_blocking=["q_1", "q_2", "q_3"],
        )
        assert explanation.reason == NoCandidatesReason.OPEN_QUESTIONS_UNRESOLVED
        assert len(explanation.question_ids_blocking) == 3
        assert len(explanation.observation_ids_referenced) == 2

    def test_serialize_to_dict(self) -> None:
        """Explanation should serialize to dict for JSON."""
        explanation = StructuredExplanation(
            reason=NoCandidatesReason.CONSTRAINT_VIOLATION,
            summary="Constraint violation occurred.",
            details={"constraint": "max_issues_per_file"},
        )
        d = explanation.to_dict()
        assert d["reason"] == "constraint_violation"
        assert d["summary"] == "Constraint violation occurred."
        assert d["details"]["constraint"] == "max_issues_per_file"

    def test_deserialize_from_dict(self) -> None:
        """Explanation should deserialize from dict."""
        data = {
            "reason": "policy_prevents_issue",
            "summary": "Policy prevents issuing.",
            "observation_ids_referenced": ["obs_123"],
        }
        explanation = StructuredExplanation.from_dict(data)
        assert explanation.reason == NoCandidatesReason.POLICY_PREVENTS_ISSUE
        assert "obs_123" in explanation.observation_ids_referenced

    def test_round_trip_serialization(self) -> None:
        """Round-trip serialization should preserve data."""
        original = StructuredExplanation(
            reason=NoCandidatesReason.EVIDENCE_CLASS_MISMATCH,
            summary="Evidence class mismatch.",
            details={"expected": "direct_code_fact", "actual": "hearsay"},
            observation_ids_referenced=["obs_a"],
            question_ids_blocking=["q_b"],
        )
        serialized = original.to_dict()
        restored = StructuredExplanation.from_dict(serialized)

        assert restored.reason == original.reason
        assert restored.summary == original.summary
        assert restored.details == original.details
        assert restored.observation_ids_referenced == original.observation_ids_referenced
        assert restored.question_ids_blocking == original.question_ids_blocking


class TestComposeIssueOutput:
    """Tests for ComposeIssueOutput contract."""

    def test_output_with_candidates(self) -> None:
        """Output with candidates is valid without explanation."""
        output = ComposeIssueOutput(
            slice_id="slice_abc123def4567890",
            task_id="task_123",
            candidate_events=[
                {"event_type": "issue.proposed", "entity_id": "issue_1"}
            ],
        )
        assert len(output.candidate_events) == 1
        assert output.explanation is None

    def test_output_with_empty_candidates_requires_explanation(self) -> None:
        """Empty candidates without explanation violates contract."""
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            ComposeIssueOutput(
                slice_id="slice_abc123def4567890",
                task_id="task_123",
                candidate_events=[],
            )
        assert "MUST include a structured explanation" in str(exc_info.value)

    def test_output_with_empty_candidates_and_explanation(self) -> None:
        """Empty candidates with explanation is valid."""
        explanation = StructuredExplanation(
            reason=NoCandidatesReason.NO_VERIFIED_OBSERVATIONS,
            summary="No verified observations available.",
        )
        output = ComposeIssueOutput(
            slice_id="slice_abc123def4567890",
            task_id="task_123",
            candidate_events=[],
            explanation=explanation,
        )
        assert output.explanation is not None
        assert output.explanation.reason == NoCandidatesReason.NO_VERIFIED_OBSERVATIONS

    def test_schema_version(self) -> None:
        """Schema version should be current."""
        output = ComposeIssueOutput(
            slice_id="slice_abc123def4567890",
            task_id="task_123",
            candidate_events=[{"event_type": "issue.proposed"}],
        )
        assert output.schema_version == SCHEMA_VERSION

    def test_serialize_to_dict(self) -> None:
        """Output should serialize correctly."""
        explanation = StructuredExplanation(
            reason=NoCandidatesReason.INSUFFICIENT_EVIDENCE,
            summary="Insufficient evidence.",
        )
        output = ComposeIssueOutput(
            slice_id="slice_abc123def4567890",
            task_id="task_123",
            candidate_events=[],
            explanation=explanation,
        )
        d = output.to_dict()
        assert d["slice_id"] == "slice_abc123def4567890"
        assert d["task_id"] == "task_123"
        assert "explanation" in d
        assert d["explanation"]["reason"] == "insufficient_evidence"

    def test_serialize_without_explanation(self) -> None:
        """Output with candidates should not include explanation in dict."""
        output = ComposeIssueOutput(
            slice_id="slice_abc123def4567890",
            task_id="task_123",
            candidate_events=[{"event_type": "issue.proposed"}],
        )
        d = output.to_dict()
        assert "explanation" not in d


class TestEnforceComposeIssueContract:
    """Tests for contract enforcement function."""

    def test_valid_output_with_candidates(self) -> None:
        """Valid output with candidates passes enforcement."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [
                {"event_type": "issue.proposed", "entity_id": "issue_1"}
            ],
        }
        result = enforce_compose_issue_contract(output)
        assert len(result.candidate_events) == 1

    def test_valid_output_with_empty_candidates_and_explanation(self) -> None:
        """Valid output with empty candidates + explanation passes."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
            "explanation": {
                "reason": "insufficient_evidence",
                "summary": "Not enough evidence to proceed.",
            },
        }
        result = enforce_compose_issue_contract(output)
        assert len(result.candidate_events) == 0
        assert result.explanation is not None
        assert result.explanation.reason == NoCandidatesReason.INSUFFICIENT_EVIDENCE

    def test_missing_required_field_raises(self) -> None:
        """Missing required fields raise contract violation."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            # missing task_id
            "candidate_events": [],
            "explanation": {"reason": "other", "summary": "Missing task_id"},
        }
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract(output)
        assert "task_id" in str(exc_info.value)

    def test_wrong_worker_role_raises(self) -> None:
        """Wrong worker_role raises contract violation."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "Reader",  # Wrong role
            "task_id": "task_123",
            "candidate_events": [],
        }
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract(output)
        assert "IssueComposer" in str(exc_info.value)

    def test_empty_candidates_without_explanation_raises(self) -> None:
        """Empty candidates without explanation raises contract violation."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
            # No explanation
        }
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract(output)
        assert "MUST include" in str(exc_info.value)
        assert "explanation" in str(exc_info.value)

    def test_explanation_missing_reason_raises(self) -> None:
        """Explanation without reason raises contract violation."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
            "explanation": {
                # missing reason
                "summary": "Some explanation",
            },
        }
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract(output)
        assert "reason" in str(exc_info.value)

    def test_explanation_missing_summary_raises(self) -> None:
        """Explanation without summary raises contract violation."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
            "explanation": {
                "reason": "other",
                # missing summary
            },
        }
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract(output)
        assert "summary" in str(exc_info.value)

    def test_invalid_reason_raises(self) -> None:
        """Invalid reason value raises contract violation."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
            "explanation": {
                "reason": "invalid_reason_code",
                "summary": "Some explanation",
            },
        }
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract(output)
        assert "Invalid explanation reason" in str(exc_info.value)

    def test_candidate_events_must_be_list(self) -> None:
        """candidate_events must be a list."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": "not a list",
        }
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract(output)
        assert "must be a list" in str(exc_info.value)


class TestClassifyEmptyCandidates:
    """Tests for classification of empty candidates."""

    def test_classify_insufficient_evidence(self) -> None:
        """Insufficient evidence maps to policy rejection."""
        output = {
            "explanation": {
                "reason": "insufficient_evidence",
                "summary": "Not enough evidence.",
                "observation_ids_referenced": ["obs_1"],
            }
        }
        detail = classify_empty_candidates(output)
        assert detail.reason == RejectionReason.POLICY_REJECTED
        assert detail.stage == RejectionStage.POLICY  # Policy rejection goes to POLICY stage
        assert "obs_1" in detail.metadata.get("observation_ids_referenced", [])

    def test_classify_no_verified_observations(self) -> None:
        """No verified observations maps to candidate empty."""
        output = {
            "explanation": {
                "reason": "no_verified_observations",
                "summary": "No verified observations.",
            }
        }
        detail = classify_empty_candidates(output)
        assert detail.reason == RejectionReason.CANDIDATE_EMPTY
        assert detail.stage == RejectionStage.CANDIDATE

    def test_classify_missing_observation(self) -> None:
        """Missing observation maps to candidate missing."""
        output = {
            "explanation": {
                "reason": "missing_required_observation",
                "summary": "Target observation missing.",
            }
        }
        detail = classify_empty_candidates(output)
        assert detail.reason == RejectionReason.CANDIDATE_MISSING
        assert detail.stage == RejectionStage.CANDIDATE

    def test_classify_open_questions(self) -> None:
        """Open questions maps to policy rejection."""
        output = {
            "explanation": {
                "reason": "open_questions_unresolved",
                "summary": "Questions unresolved.",
                "question_ids_blocking": ["q_1", "q_2"],
            }
        }
        detail = classify_empty_candidates(output)
        assert detail.reason == RejectionReason.POLICY_REJECTED
        assert detail.stage == RejectionStage.POLICY  # Policy rejection goes to POLICY stage
        assert "q_1" in detail.metadata.get("question_ids_blocking", [])

    def test_classify_other(self) -> None:
        """Other reason maps to policy rejection."""
        output = {
            "explanation": {
                "reason": "other",
                "summary": "Custom reason for failure.",
                "details": {"context": "specific situation"},
            }
        }
        detail = classify_empty_candidates(output)
        assert detail.reason == RejectionReason.POLICY_REJECTED
        assert detail.stage == RejectionStage.POLICY  # Policy rejection goes to POLICY stage
        assert "Custom reason" in detail.message


class TestFactoryFunctions:
    """Tests for explanation factory functions."""

    def test_create_insufficient_evidence_explanation(self) -> None:
        """Factory creates correct insufficient evidence explanation."""
        explanation = create_insufficient_evidence_explanation(
            observation_id="obs_123",
            evidence_class="hearsay",
            required_classes=["direct_code_fact"],
        )
        assert explanation.reason == NoCandidatesReason.INSUFFICIENT_EVIDENCE
        assert "obs_123" in explanation.observation_ids_referenced
        assert explanation.details["evidence_class"] == "hearsay"

    def test_create_no_verified_observations_explanation(self) -> None:
        """Factory creates correct no verified observations explanation."""
        explanation = create_no_verified_observations_explanation(
            target_observation_id="obs_target"
        )
        assert explanation.reason == NoCandidatesReason.NO_VERIFIED_OBSERVATIONS
        assert "obs_target" in explanation.observation_ids_referenced

    def test_create_open_questions_explanation(self) -> None:
        """Factory creates correct open questions explanation."""
        explanation = create_open_questions_explanation(
            question_ids=["q_1", "q_2"],
            questions_summary="Authentication and authorization unclear",
        )
        assert explanation.reason == NoCandidatesReason.OPEN_QUESTIONS_UNRESOLVED
        assert len(explanation.question_ids_blocking) == 2
        assert "Authentication" in explanation.summary

    def test_create_other_explanation(self) -> None:
        """Factory creates correct other explanation."""
        explanation = create_other_explanation(
            summary="Custom situation occurred.",
            details={"key": "value"},
        )
        assert explanation.reason == NoCandidatesReason.OTHER
        assert explanation.details["key"] == "value"


class TestStep5DoD:
    """Tests for STEP 5 Definition of Done criteria."""

    def test_no_silent_empty_output(self) -> None:
        """DoD: No silent empty output - every empty must have explanation."""
        # This should raise - no silent empty allowed
        with pytest.raises(ComposeIssueContractViolation):
            enforce_compose_issue_contract({
                "schema_version": "1.1.0",
                "slice_id": "slice_abc123def4567890",
                "worker_role": "IssueComposer",
                "task_id": "task_123",
                "candidate_events": [],
            })

    def test_every_case_has_explicit_reason(self) -> None:
        """DoD: Every case has explicit reason."""
        # Valid empty case has reason
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
            "explanation": {
                "reason": "insufficient_evidence",
                "summary": "Evidence was insufficient.",
            },
        }
        result = enforce_compose_issue_contract(output)
        assert result.explanation is not None
        assert result.explanation.reason is not None

    def test_non_json_would_be_classified(self) -> None:
        """DoD: Non-JSON is visible and classified.

        This is tested at the parse stage level (STEP 0/3), but the contract
        enforcement catches missing required fields which would indicate
        parse issues.
        """
        # Missing fields indicate non-conformant JSON
        with pytest.raises(ComposeIssueContractViolation) as exc_info:
            enforce_compose_issue_contract({
                "slice_id": "slice_abc123def4567890",
                # Missing other required fields
            })
        assert "missing" in str(exc_info.value).lower()

    def test_acceptance_funnel_transparent(self) -> None:
        """DoD: Acceptance funnel is fully transparent.

        The explanation provides full transparency into why no candidates
        were produced, including referenced observations and blocking questions.
        """
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
            "explanation": {
                "reason": "open_questions_unresolved",
                "summary": "Critical auth questions unanswered.",
                "observation_ids_referenced": ["obs_1", "obs_2"],
                "question_ids_blocking": ["q_auth_1", "q_auth_2"],
                "details": {
                    "auth_mechanism": "unknown",
                    "token_validation": "unclear",
                },
            },
        }
        result = enforce_compose_issue_contract(output)

        # Full transparency
        assert result.explanation is not None
        assert len(result.explanation.observation_ids_referenced) == 2
        assert len(result.explanation.question_ids_blocking) == 2
        assert "auth_mechanism" in result.explanation.details

    def test_all_reason_codes_valid(self) -> None:
        """DoD: All reason codes are valid and documented."""
        valid_reasons = [
            "insufficient_evidence",
            "evidence_class_mismatch",
            "missing_required_observation",
            "open_questions_unresolved",
            "constraint_violation",
            "no_verified_observations",
            "policy_prevents_issue",
            "other",
        ]
        for reason in valid_reasons:
            output = {
                "schema_version": "1.1.0",
                "slice_id": "slice_abc123def4567890",
                "worker_role": "IssueComposer",
                "task_id": "task_123",
                "candidate_events": [],
                "explanation": {
                    "reason": reason,
                    "summary": f"Test for {reason}",
                },
            }
            result = enforce_compose_issue_contract(output)
            assert result.explanation is not None


class TestContractViolationDetails:
    """Tests that contract violations include helpful details."""

    def test_violation_includes_task_id(self) -> None:
        """Contract violation should include task_id for debugging."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_special_123",
            "candidate_events": [],
        }
        try:
            enforce_compose_issue_contract(output)
            assert False, "Should have raised"
        except ComposeIssueContractViolation as e:
            assert e.task_id == "task_special_123"

    def test_violation_includes_slice_id(self) -> None:
        """Contract violation should include slice_id for debugging."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_special_456",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
        }
        try:
            enforce_compose_issue_contract(output)
            assert False, "Should have raised"
        except ComposeIssueContractViolation as e:
            assert e.slice_id == "slice_special_456"

    def test_violation_includes_details(self) -> None:
        """Contract violation should include details dict."""
        output = {
            "schema_version": "1.1.0",
            "slice_id": "slice_abc123def4567890",
            "worker_role": "IssueComposer",
            "task_id": "task_123",
            "candidate_events": [],
        }
        try:
            enforce_compose_issue_contract(output)
            assert False, "Should have raised"
        except ComposeIssueContractViolation as e:
            assert "candidate_count" in e.details
            assert "explanation_present" in e.details
