"""
Unit tests for the Rejection Classification Layer (STEP 0)

These tests verify that:
1. Every rejected candidate has a classified rejection_reason
2. No terminal no-output state exists without a reason chain
3. Rejection classification happens at the correct pipeline stage
"""

from __future__ import annotations

import pytest

from runtime.rejection import (
    REJECTION_REASON_TO_STAGE,
    RejectionChain,
    RejectionDetail,
    RejectionEvent,
    RejectionInvariantViolation,
    RejectionReason,
    RejectionStage,
    classify_candidate_empty_rejection,
    classify_candidate_missing_rejection,
    classify_parse_rejection,
    classify_policy_rejection,
    classify_schema_rejection,
    classify_transport_rejection,
    create_completed_no_events_explanation,
    enforce_rejection_invariant,
)


class TestRejectionReasonEnum:
    """Tests for RejectionReason enum completeness."""

    def test_required_reasons_exist(self) -> None:
        """Verify all required rejection reasons are defined."""
        required_reasons = {
            "parse_non_json",
            "schema_invalid",
            "candidate_missing",
            "candidate_empty",
            "policy_rejected",
            "transport_rejected",
        }
        actual_reasons = {reason.value for reason in RejectionReason}
        assert required_reasons <= actual_reasons, (
            f"Missing required reasons: {required_reasons - actual_reasons}"
        )

    def test_all_reasons_have_stage_mapping(self) -> None:
        """Verify every rejection reason maps to a stage."""
        for reason in RejectionReason:
            assert reason in REJECTION_REASON_TO_STAGE, (
                f"RejectionReason.{reason.name} not in REJECTION_REASON_TO_STAGE"
            )


class TestRejectionStageEnum:
    """Tests for RejectionStage enum completeness."""

    def test_required_stages_exist(self) -> None:
        """Verify all pipeline stages are defined."""
        required_stages = {"parse", "schema", "candidate", "policy", "transport"}
        actual_stages = {stage.value for stage in RejectionStage}
        assert required_stages == actual_stages, (
            f"Stage mismatch. Expected: {required_stages}, Got: {actual_stages}"
        )


class TestRejectionDetail:
    """Tests for RejectionDetail dataclass."""

    def test_rejection_detail_requires_reason_stage_message(self) -> None:
        """Verify rejection detail has all required fields."""
        detail = RejectionDetail(
            reason=RejectionReason.PARSE_NON_JSON,
            stage=RejectionStage.PARSE,
            message="Test message",
        )
        assert detail.reason == RejectionReason.PARSE_NON_JSON
        assert detail.stage == RejectionStage.PARSE
        assert detail.message == "Test message"

    def test_rejection_detail_validates_stage_consistency(self) -> None:
        """Verify rejection detail validates stage matches reason."""
        # This should work - correct stage for reason
        detail = RejectionDetail(
            reason=RejectionReason.SCHEMA_INVALID,
            stage=RejectionStage.SCHEMA,
            message="Schema error",
        )
        assert detail.stage == RejectionStage.SCHEMA

        # This should fail - wrong stage for reason
        with pytest.raises(ValueError, match="must map to"):
            RejectionDetail(
                reason=RejectionReason.SCHEMA_INVALID,
                stage=RejectionStage.PARSE,  # Wrong!
                message="Schema error",
            )

    def test_to_dict_includes_all_fields(self) -> None:
        """Verify serialization includes all relevant fields."""
        detail = RejectionDetail(
            reason=RejectionReason.TRANSPORT_REJECTED,
            stage=RejectionStage.TRANSPORT,
            message="Missing line_start",
            schema_path="evidence[0].line_start",
            validator_code="required_field_missing",
        )
        d = detail.to_dict()
        assert d["reason"] == "transport_rejected"
        assert d["stage"] == "transport"
        assert d["message"] == "Missing line_start"
        assert d["schema_path"] == "evidence[0].line_start"
        assert d["validator_code"] == "required_field_missing"


class TestRejectionChain:
    """Tests for RejectionChain."""

    def test_empty_chain_has_no_reason_chain(self) -> None:
        """Verify empty chain reports no reason chain."""
        chain = RejectionChain(task_id="test", worker_role="Reader")
        assert not chain.has_reason_chain

    def test_chain_with_rejections_has_reason_chain(self) -> None:
        """Verify chain with rejections reports reason chain."""
        chain = RejectionChain(task_id="test", worker_role="Reader")
        chain.add_rejection(RejectionDetail(
            reason=RejectionReason.SCHEMA_INVALID,
            stage=RejectionStage.SCHEMA,
            message="Test",
        ))
        assert chain.has_reason_chain

    def test_chain_with_parse_failure_has_reason_chain(self) -> None:
        """Verify chain with parse failure reports reason chain."""
        chain = RejectionChain(task_id="test", worker_role="Reader")
        chain.set_parse_failure(classify_parse_rejection("{bad", "Invalid JSON"))
        assert chain.has_reason_chain

    def test_stage_summary_counts_correctly(self) -> None:
        """Verify stage summary counts rejections by stage."""
        chain = RejectionChain(task_id="test", worker_role="Reader")
        chain.add_rejection(classify_schema_rejection("p1", "m1"))
        chain.add_rejection(classify_schema_rejection("p2", "m2"))
        chain.add_rejection(classify_policy_rejection("m3"))

        summary = chain.stage_summary
        assert summary.get(RejectionStage.SCHEMA) == 2
        assert summary.get(RejectionStage.POLICY) == 1


class TestClassifyParseRejection:
    """Tests for classify_parse_rejection."""

    def test_classify_parse_rejection_basic(self) -> None:
        """Verify parse rejection classification."""
        detail = classify_parse_rejection("{not valid json", "Expecting property name")
        assert detail.reason == RejectionReason.PARSE_NON_JSON
        assert detail.stage == RejectionStage.PARSE
        assert "Expecting property name" in detail.message
        assert detail.raw_output_excerpt == "{not valid json"

    def test_classify_parse_rejection_truncates_excerpt(self) -> None:
        """Verify long output is truncated."""
        long_output = "x" * 500
        detail = classify_parse_rejection(long_output, "Error", output_excerpt_length=100)
        assert len(detail.raw_output_excerpt) == 100

    def test_classify_parse_rejection_handles_none(self) -> None:
        """Verify None output is handled."""
        detail = classify_parse_rejection(None, "Output is None")
        assert detail.reason == RejectionReason.PARSE_NON_JSON
        assert detail.raw_output_excerpt is None


class TestClassifySchemaRejection:
    """Tests for classify_schema_rejection."""

    def test_classify_schema_rejection_basic(self) -> None:
        """Verify schema rejection classification."""
        detail = classify_schema_rejection("payload.claim", "Missing required field")
        assert detail.reason == RejectionReason.SCHEMA_INVALID
        assert detail.stage == RejectionStage.SCHEMA
        assert detail.schema_path == "payload.claim"

    def test_classify_schema_rejection_with_validator_code(self) -> None:
        """Verify validator code is preserved."""
        detail = classify_schema_rejection("p", "m", validator_code="required_field")
        assert detail.validator_code == "required_field"


class TestClassifyCandidateRejection:
    """Tests for candidate stage rejection classification."""

    def test_classify_candidate_missing(self) -> None:
        """Verify missing candidate_events classification."""
        detail = classify_candidate_missing_rejection()
        assert detail.reason == RejectionReason.CANDIDATE_MISSING
        assert detail.stage == RejectionStage.CANDIDATE

    def test_classify_candidate_empty(self) -> None:
        """Verify empty candidate_events classification."""
        detail = classify_candidate_empty_rejection(candidate_count=0)
        assert detail.reason == RejectionReason.CANDIDATE_EMPTY
        assert detail.stage == RejectionStage.CANDIDATE
        assert detail.metadata["candidate_count"] == 0


class TestClassifyPolicyRejection:
    """Tests for classify_policy_rejection."""

    def test_classify_policy_rejection_basic(self) -> None:
        """Verify policy rejection classification."""
        detail = classify_policy_rejection("Low confidence score")
        assert detail.reason == RejectionReason.POLICY_REJECTED
        assert detail.stage == RejectionStage.POLICY

    def test_classify_policy_rejection_with_metadata(self) -> None:
        """Verify policy metadata is preserved."""
        detail = classify_policy_rejection(
            "Below threshold",
            validator_code="low_confidence",
            policy_name="low_noise",
            confidence=0.45,
        )
        assert detail.validator_code == "low_confidence"
        assert detail.metadata["policy_name"] == "low_noise"
        assert detail.metadata["confidence"] == 0.45


class TestClassifyTransportRejection:
    """Tests for classify_transport_rejection."""

    def test_classify_transport_rejection_basic(self) -> None:
        """Verify transport rejection classification."""
        detail = classify_transport_rejection(
            "Missing line_start in evidence",
            field_path="evidence[0].line_start",
        )
        assert detail.reason == RejectionReason.TRANSPORT_REJECTED
        assert detail.stage == RejectionStage.TRANSPORT
        assert detail.schema_path == "evidence[0].line_start"


class TestEnforceRejectionInvariant:
    """Tests for rejection invariant enforcement."""

    def test_invariant_satisfied_with_accepted(self) -> None:
        """Verify invariant passes when candidates accepted."""
        # Should not raise
        enforce_rejection_invariant(
            has_accepted=True,
            rejection_chain=None,
            task_id="test",
            worker_role="Reader",
        )

    def test_invariant_satisfied_with_rejection_chain(self) -> None:
        """Verify invariant passes when rejection chain exists."""
        chain = RejectionChain(task_id="test", worker_role="Reader")
        chain.add_rejection(classify_schema_rejection("p", "m"))

        # Should not raise
        enforce_rejection_invariant(
            has_accepted=False,
            rejection_chain=chain,
            task_id="test",
            worker_role="Reader",
        )

    def test_invariant_violated_without_reason_chain(self) -> None:
        """Verify invariant fails when no reason chain exists."""
        with pytest.raises(RejectionInvariantViolation, match="no rejection reason chain"):
            enforce_rejection_invariant(
                has_accepted=False,
                rejection_chain=None,
                task_id="test",
                worker_role="Reader",
            )

    def test_invariant_violated_with_empty_chain(self) -> None:
        """Verify invariant fails when chain is empty."""
        chain = RejectionChain(task_id="test", worker_role="Reader")
        # chain has no rejections

        with pytest.raises(RejectionInvariantViolation):
            enforce_rejection_invariant(
                has_accepted=False,
                rejection_chain=chain,
                task_id="test",
                worker_role="Reader",
            )


class TestCompletedNoEventsExplanation:
    """Tests for completed_no_events explanation."""

    def test_explanation_includes_all_fields(self) -> None:
        """Verify explanation includes complete rejection info."""
        chain = RejectionChain(task_id="task_123", worker_role="Reader")
        chain.add_rejection(classify_schema_rejection("p1", "m1"))
        chain.add_rejection(classify_policy_rejection("m2"))

        explanation = create_completed_no_events_explanation(chain)

        assert explanation["status"] == "completed_no_events"
        assert explanation["task_id"] == "task_123"
        assert explanation["worker_role"] == "Reader"
        assert explanation["total_candidates"] == 2
        assert "schema" in explanation["stage_breakdown"]
        assert "policy" in explanation["stage_breakdown"]
        assert "reason_chain" in explanation


class TestRejectionEvent:
    """Tests for RejectionEvent schema."""

    def test_rejection_event_serialization(self) -> None:
        """Verify RejectionEvent serializes correctly."""
        event = RejectionEvent(
            rejection_reason=RejectionReason.TRANSPORT_REJECTED,
            rejection_stage=RejectionStage.TRANSPORT,
            rejection_message="Missing line_start",
            task_id="task_123",
            schema_path="evidence[0].line_start",
        )
        d = event.to_dict()

        assert d["event_type"] == "candidate.rejected"
        assert d["rejection_reason"] == "transport_rejected"
        assert d["rejection_stage"] == "transport"
        assert d["task_id"] == "task_123"

    def test_rejection_event_with_defaults(self) -> None:
        """Verify RejectionEvent with minimal fields."""
        event = RejectionEvent(
            rejection_reason=RejectionReason.CANDIDATE_EMPTY,
            rejection_stage=RejectionStage.CANDIDATE,
            rejection_message="No candidates produced",
        )
        d = event.to_dict()

        assert d["event_type"] == "candidate.rejected"
        assert d["candidate_id"] is None
        assert d["task_id"] is None
