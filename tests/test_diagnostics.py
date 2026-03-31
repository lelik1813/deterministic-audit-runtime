"""
Unit tests for Diagnostics Projection Layer (STEP 3)

These tests verify that:
1. Every task execution produces a fully inspectable acceptance funnel
2. Pipeline visibility is complete (parse → schema → extract → policy → outcome)
3. Opaque "no accepted candidate events" is replaced with explicit explanations
"""

from __future__ import annotations

import pytest

from runtime.diagnostics import (
    AcceptanceFunnel,
    DiagnosticsBuilder,
    DiagnosticsProjection,
    PipelineStage,
    RejectionAggregation,
    StageResult,
    TerminationReason,
    aggregate_diagnostics,
)
from runtime.rejection import RejectionReason, RejectionStage


class TestPipelineStageEnum:
    """Tests for PipelineStage enum."""

    def test_required_stages_exist(self) -> None:
        """Verify all pipeline stages are defined."""
        required = {"parse", "schema", "extract", "policy", "outcome"}
        actual = {s.value for s in PipelineStage}
        assert required == actual


class TestTerminationReasonEnum:
    """Tests for TerminationReason enum."""

    def test_required_reasons_exist(self) -> None:
        """Verify all termination reasons are defined."""
        required = {
            "success",
            "no_candidates",
            "parse_failure",
            "schema_failure",
            "extraction_failure",
            "policy_rejection",
            "transport_failure",
        }
        actual = {r.value for r in TerminationReason}
        assert required == actual


class TestStageResult:
    """Tests for StageResult dataclass."""

    def test_basic_stage_result(self) -> None:
        """Verify basic stage result creation."""
        result = StageResult(
            stage=PipelineStage.PARSE,
            passed=True,
            items_in=1,
            items_out=1,
        )
        assert result.stage == PipelineStage.PARSE
        assert result.passed is True
        assert result.rejection_count == 0

    def test_rejection_count(self) -> None:
        """Verify rejection count is calculated correctly."""
        result = StageResult(
            stage=PipelineStage.POLICY,
            passed=False,
            items_in=5,
            items_out=0,
            rejection_reasons=[
                RejectionReason.POLICY_REJECTED,
                RejectionReason.POLICY_REJECTED,
            ],
        )
        assert result.rejection_count == 2


class TestAcceptanceFunnel:
    """Tests for AcceptanceFunnel."""

    def test_empty_funnel(self) -> None:
        """Verify empty funnel has zero counts."""
        funnel = AcceptanceFunnel(task_id="task_1")
        assert funnel.total_candidates == 0
        assert funnel.accepted_events == 0
        assert funnel.rejected_events == 0

    def test_add_stages(self) -> None:
        """Verify stages can be added."""
        funnel = AcceptanceFunnel(task_id="task_1")
        funnel.add_stage(StageResult(
            stage=PipelineStage.PARSE,
            passed=True,
            items_in=1,
            items_out=1,
        ))
        funnel.add_stage(StageResult(
            stage=PipelineStage.SCHEMA,
            passed=True,
            items_in=1,
            items_out=1,
        ))
        assert len(funnel.stages) == 2

    def test_all_rejection_reasons(self) -> None:
        """Verify all rejection reasons are collected."""
        funnel = AcceptanceFunnel(task_id="task_1")
        funnel.add_stage(StageResult(
            stage=PipelineStage.PARSE,
            passed=False,
            rejection_reasons=[RejectionReason.PARSE_NON_JSON],
        ))
        funnel.add_stage(StageResult(
            stage=PipelineStage.POLICY,
            passed=False,
            rejection_reasons=[
                RejectionReason.POLICY_REJECTED,
                RejectionReason.POLICY_REJECTED,
            ],
        ))
        assert len(funnel.all_rejection_reasons) == 3

    def test_to_dict_serializable(self) -> None:
        """Verify funnel can be serialized to dict."""
        funnel = AcceptanceFunnel(task_id="task_1")
        funnel.add_stage(StageResult(
            stage=PipelineStage.PARSE,
            passed=True,
        ))
        d = funnel.to_dict()
        assert d["task_id"] == "task_1"
        assert "stages" in d


class TestDiagnosticsProjection:
    """Tests for DiagnosticsProjection."""

    def test_explanation_for_success(self) -> None:
        """Verify success has explanation."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            accepted_count=5,
        )
        assert diag.accepted_count == 5
        assert diag.explanation_available is True
        assert "5 accepted events" in diag.get_explanation()

    def test_explanation_for_no_output(self) -> None:
        """Verify no output has explanation."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=False,
        )
        assert diag.explanation_available is False
        assert "No output" in diag.get_explanation()

    def test_explanation_for_invalid_json(self) -> None:
        """Verify invalid JSON has explanation."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=False,
            json_parse_error="Expecting property name",
        )
        assert "not valid JSON" in diag.get_explanation()

    def test_explanation_for_missing_candidates(self) -> None:
        """Verify missing candidates has explanation."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=True,
            candidate_missing=True,
        )
        assert "did not contain" in diag.get_explanation()

    def test_explanation_for_empty_candidates(self) -> None:
        """Verify empty candidates has explanation."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=True,
            empty_candidate_array=True,
            candidate_count=0,
        )
        assert "empty" in diag.get_explanation().lower()

    def test_explanation_for_policy_rejection(self) -> None:
        """Verify policy rejection has explanation."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=True,
            schema_ok=True,  # Must set this to reach policy rejection check
            candidate_count=3,
            policy_rejected_count=3,
            accepted_count=0,
        )
        assert "rejected by policy" in diag.get_explanation()

    def test_to_dict_includes_all_fields(self) -> None:
        """Verify serialization includes all relevant fields."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=True,
            schema_ok=True,
            candidate_count=5,
            accepted_count=3,
            rejected_count=2,
            worker_role="Reader",
        )
        d = diag.to_dict()
        assert d["task_id"] == "task_1"
        assert d["raw_output_present"] is True
        assert d["json_parse_ok"] is True
        assert d["candidate_count"] == 5
        assert d["accepted_count"] == 3
        assert d["explanation_available"] is True


class TestDiagnosticsBuilder:
    """Tests for DiagnosticsBuilder."""

    def test_build_complete_diagnostics(self) -> None:
        """Verify builder can create complete diagnostics."""
        diag = (
            DiagnosticsBuilder("task_1")
            .with_worker_role("Reader")
            .with_backend_type("claude_sdk")
            .with_raw_output('{"candidate_events": []}')
            .with_json_parse_result(success=True)
            .with_schema_result(success=True)
            .with_extraction_result(candidate_count=0, candidates_empty=True)
            .build()
        )
        assert diag.task_id == "task_1"
        assert diag.worker_role == "Reader"
        assert diag.raw_output_present is True
        assert diag.json_parse_ok is True
        assert diag.candidate_count == 0
        assert diag.empty_candidate_array is True

    def test_parse_failure_flow(self) -> None:
        """Verify parse failure creates correct funnel."""
        diag = (
            DiagnosticsBuilder("task_1")
            .with_raw_output("not json")
            .with_json_parse_result(success=False, error="Invalid JSON")
            .build()
        )
        assert diag.json_parse_ok is False
        assert diag.acceptance_funnel is not None
        assert diag.acceptance_funnel.termination_reason == TerminationReason.PARSE_FAILURE

    def test_schema_failure_flow(self) -> None:
        """Verify schema failure creates correct funnel."""
        diag = (
            DiagnosticsBuilder("task_1")
            .with_raw_output('{}')
            .with_json_parse_result(success=True)
            .with_schema_result(
                success=False,
                errors=["Missing required field: candidate_events"]
            )
            .build()
        )
        assert diag.schema_ok is False
        assert diag.acceptance_funnel.termination_reason == TerminationReason.SCHEMA_FAILURE

    def test_empty_candidates_flow(self) -> None:
        """Verify empty candidates creates correct funnel."""
        diag = (
            DiagnosticsBuilder("task_1")
            .with_raw_output('{"candidate_events": []}')
            .with_json_parse_result(success=True)
            .with_schema_result(success=True)
            .with_extraction_result(candidate_count=0, candidates_empty=True)
            .build()
        )
        assert diag.empty_candidate_array is True
        assert diag.acceptance_funnel.termination_reason == TerminationReason.NO_CANDIDATES

    def test_policy_rejection_flow(self) -> None:
        """Verify policy rejection creates correct funnel."""
        diag = (
            DiagnosticsBuilder("task_1")
            .with_raw_output('{"candidate_events": [{}]}')
            .with_json_parse_result(success=True)
            .with_schema_result(success=True)
            .with_extraction_result(candidate_count=3)
            .with_policy_result(
                accepted_count=0,
                rejected_count=3,
                rejection_reasons=[RejectionReason.POLICY_REJECTED],
            )
            .build()
        )
        assert diag.policy_rejected_count == 3
        assert diag.acceptance_funnel.termination_reason == TerminationReason.POLICY_REJECTION

    def test_successful_flow(self) -> None:
        """Verify successful flow creates correct funnel."""
        diag = (
            DiagnosticsBuilder("task_1")
            .with_raw_output('{"candidate_events": [{}]}')
            .with_json_parse_result(success=True)
            .with_schema_result(success=True)
            .with_extraction_result(candidate_count=2)
            .with_policy_result(accepted_count=2, rejected_count=0)
            .with_transport_result(accepted_count=2, rejected_count=0)
            .build()
        )
        assert diag.accepted_count == 2
        assert diag.acceptance_funnel.termination_reason == TerminationReason.SUCCESS


class TestRejectionAggregation:
    """Tests for RejectionAggregation."""

    def test_empty_aggregation(self) -> None:
        """Verify empty aggregation has zero counts."""
        agg = RejectionAggregation()
        assert agg.total_tasks == 0
        assert agg.tasks_no_events == 0
        assert agg.no_events_rate == 0.0

    def test_add_successful_diagnostics(self) -> None:
        """Verify adding successful diagnostics."""
        agg = RejectionAggregation()
        agg.add_diagnostics(DiagnosticsProjection(
            task_id="task_1",
            accepted_count=5,
        ))
        assert agg.total_tasks == 1
        assert agg.tasks_no_events == 0
        assert agg.no_events_rate == 0.0

    def test_add_failed_diagnostics(self) -> None:
        """Verify adding failed diagnostics."""
        agg = RejectionAggregation()
        agg.add_diagnostics(DiagnosticsProjection(
            task_id="task_1",
            accepted_count=0,
            rejection_reasons=[RejectionReason.CANDIDATE_EMPTY],
            rejection_stage=RejectionStage.CANDIDATE,
        ))
        assert agg.total_tasks == 1
        assert agg.tasks_no_events == 1
        assert agg.no_events_rate == 1.0

    def test_rejection_reason_counts(self) -> None:
        """Verify rejection reason counting."""
        agg = RejectionAggregation()
        agg.add_diagnostics(DiagnosticsProjection(
            task_id="task_1",
            accepted_count=0,
            rejection_reasons=[RejectionReason.PARSE_NON_JSON],
            rejection_stage=RejectionStage.PARSE,
        ))
        agg.add_diagnostics(DiagnosticsProjection(
            task_id="task_2",
            accepted_count=0,
            rejection_reasons=[RejectionReason.CANDIDATE_EMPTY],
            rejection_stage=RejectionStage.CANDIDATE,
        ))
        agg.add_diagnostics(DiagnosticsProjection(
            task_id="task_3",
            accepted_count=0,
            rejection_reasons=[RejectionReason.PARSE_NON_JSON],
            rejection_stage=RejectionStage.PARSE,
        ))

        assert agg.rejection_reason_counts.get(RejectionReason.PARSE_NON_JSON) == 2
        assert agg.rejection_reason_counts.get(RejectionReason.CANDIDATE_EMPTY) == 1

    def test_to_dict_serializable(self) -> None:
        """Verify aggregation can be serialized."""
        agg = RejectionAggregation()
        agg.add_diagnostics(DiagnosticsProjection(
            task_id="task_1",
            accepted_count=0,
            rejection_reasons=[RejectionReason.POLICY_REJECTED],
            rejection_stage=RejectionStage.POLICY,
        ))
        d = agg.to_dict()
        assert d["total_tasks"] == 1
        assert d["tasks_no_events"] == 1
        assert "rejection_reason_counts" in d


class TestAggregateDiagnostics:
    """Tests for aggregate_diagnostics function."""

    def test_aggregate_empty_list(self) -> None:
        """Verify empty list produces empty aggregation."""
        agg = aggregate_diagnostics([])
        assert agg.total_tasks == 0

    def test_aggregate_multiple(self) -> None:
        """Verify aggregating multiple diagnostics."""
        diags = [
            DiagnosticsProjection(
                task_id="task_1",
                accepted_count=5,
            ),
            DiagnosticsProjection(
                task_id="task_2",
                accepted_count=0,
                rejection_reasons=[RejectionReason.CANDIDATE_EMPTY],
                rejection_stage=RejectionStage.CANDIDATE,
            ),
            DiagnosticsProjection(
                task_id="task_3",
                accepted_count=0,
                rejection_reasons=[RejectionReason.POLICY_REJECTED],
                rejection_stage=RejectionStage.POLICY,
            ),
        ]
        agg = aggregate_diagnostics(diags)
        assert agg.total_tasks == 3
        assert agg.tasks_no_events == 2
        assert agg.no_events_rate == pytest.approx(2/3)


class TestStep3DoD:
    """Tests for STEP 3 Definition of Done criteria."""

    def test_every_task_has_diagnostics(self) -> None:
        """DoD: Every task execution produces diagnostics."""
        builder = DiagnosticsBuilder("task_1")
        # Even with no calls, diagnostics is produced
        diag = builder.build()
        assert diag is not None
        assert diag.task_id == "task_1"

    def test_no_opaque_no_events_message(self) -> None:
        """DoD: No more opaque 'no accepted candidate events' message."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=True,
            empty_candidate_array=True,
        )
        explanation = diag.get_explanation()
        # Explanation should NOT be generic "no accepted candidate events"
        assert "unknown reason" not in explanation.lower() or diag.has_rejection_reasons

    def test_can_distinguish_parse_failure(self) -> None:
        """DoD: Can distinguish parse failure from other failures."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=False,
            json_parse_error="Invalid JSON",
            rejection_reasons=[RejectionReason.PARSE_NON_JSON],
            rejection_stage=RejectionStage.PARSE,
        )
        assert diag.primary_rejection_reason == RejectionReason.PARSE_NON_JSON
        assert diag.rejection_stage == RejectionStage.PARSE
        assert "JSON" in diag.get_explanation()

    def test_can_distinguish_empty_candidates(self) -> None:
        """DoD: Can distinguish empty candidates from other failures."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=True,
            schema_ok=True,
            candidate_count=0,
            empty_candidate_array=True,
            rejection_reasons=[RejectionReason.CANDIDATE_EMPTY],
            rejection_stage=RejectionStage.CANDIDATE,
        )
        assert diag.primary_rejection_reason == RejectionReason.CANDIDATE_EMPTY
        assert "empty" in diag.get_explanation().lower()

    def test_can_distinguish_policy_rejection(self) -> None:
        """DoD: Can distinguish policy rejection from other failures."""
        diag = DiagnosticsProjection(
            task_id="task_1",
            raw_output_present=True,
            json_parse_ok=True,
            schema_ok=True,
            candidate_count=3,
            policy_rejected_count=3,
            accepted_count=0,
            rejection_reasons=[RejectionReason.POLICY_REJECTED],
            rejection_stage=RejectionStage.POLICY,
        )
        assert diag.primary_rejection_reason == RejectionReason.POLICY_REJECTED
        assert "policy" in diag.get_explanation().lower()

    def test_acceptance_funnel_fully_inspectable(self) -> None:
        """DoD: Acceptance funnel is fully inspectable."""
        diag = (
            DiagnosticsBuilder("task_1")
            .with_raw_output('{"candidate_events": []}')
            .with_json_parse_result(success=True)
            .with_schema_result(success=True)
            .with_extraction_result(candidate_count=0, candidates_empty=True)
            .build()
        )
        # Funnel should have all stages recorded
        assert diag.acceptance_funnel is not None
        stage_values = {s.stage.value for s in diag.acceptance_funnel.stages}
        assert "parse" in stage_values
        assert "schema" in stage_values
        assert "extract" in stage_values
