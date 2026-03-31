"""
Diagnostics Projection Layer (STEP 3)

This module implements the diagnostics projection layer that provides full
visibility into the acceptance funnel for every task execution.

Core Invariant:
Every task execution MUST produce a fully inspectable acceptance funnel

Pipeline visibility:
    parse → schema → extract → policy → outcome

Diagnostics per attempt:
    - raw_output_present: bool
    - json_parse_ok: bool
    - schema_ok: bool
    - candidate_count: int
    - accepted_count: int
    - rejection_stage: RejectionStage | None
    - rejection_reasons: list[RejectionReason]
    - empty_candidate_array: bool
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.rejection import (
    RejectionReason,
    RejectionStage,
    RejectionChain,
)


class PipelineStage(Enum):
    """Pipeline stages for acceptance funnel visualization."""
    PARSE = "parse"
    SCHEMA = "schema"
    EXTRACT = "extract"
    POLICY = "policy"
    OUTCOME = "outcome"


class TerminationReason(Enum):
    """Why the pipeline terminated at a specific stage."""
    SUCCESS = "success"
    """Pipeline completed successfully with accepted events."""

    NO_CANDIDATES = "no_candidates"
    """Pipeline completed but no candidates were produced."""

    PARSE_FAILURE = "parse_failure"
    """Pipeline terminated at parse stage (invalid JSON)."""

    SCHEMA_FAILURE = "schema_failure"
    """Pipeline terminated at schema stage (invalid structure)."""

    EXTRACTION_FAILURE = "extraction_failure"
    """Pipeline terminated at extract stage (no candidate_events)."""

    POLICY_REJECTION = "policy_rejection"
    """Pipeline terminated at policy stage (all candidates rejected)."""

    TRANSPORT_FAILURE = "transport_failure"
    """Pipeline terminated at transport stage (validation failure)."""


@dataclass
class StageResult:
    """Result of a single pipeline stage."""
    stage: PipelineStage
    """Which stage this is."""

    passed: bool
    """Whether the stage passed."""

    items_in: int = 0
    """Number of items entering this stage."""

    items_out: int = 0
    """Number of items leaving this stage."""

    rejection_reasons: list[RejectionReason] = field(default_factory=list)
    """Rejection reasons at this stage."""

    details: dict[str, Any] = field(default_factory=dict)
    """Additional stage-specific details."""

    @property
    def rejection_count(self) -> int:
        """Number of rejections at this stage."""
        return len(self.rejection_reasons)


@dataclass
class AcceptanceFunnel:
    """
    Complete acceptance funnel for a task execution.

    This provides stage-by-stage visibility into why a task
    produced (or didn't produce) accepted events.

    The funnel tracks:
    - How many items entered each stage
    - How many passed each stage
    - Why items were rejected at each stage
    """
    task_id: str
    """ID of the task."""

    worker_role: str | None = None
    """Worker role that was executed."""

    # Stage results in order
    stages: list[StageResult] = field(default_factory=list)
    """Results for each pipeline stage."""

    termination_reason: TerminationReason = TerminationReason.SUCCESS
    """Why the pipeline terminated."""

    termination_stage: PipelineStage | None = None
    """At which stage the pipeline terminated."""

    @property
    def total_candidates(self) -> int:
        """Total candidates that entered extraction."""
        extract_stage = self._get_stage(PipelineStage.EXTRACT)
        if extract_stage:
            return extract_stage.items_in
        return 0

    @property
    def accepted_events(self) -> int:
        """Total events accepted."""
        outcome_stage = self._get_stage(PipelineStage.OUTCOME)
        if outcome_stage:
            return outcome_stage.items_out
        return 0

    @property
    def rejected_events(self) -> int:
        """Total events rejected across all stages."""
        return sum(stage.rejection_count for stage in self.stages)

    @property
    def all_rejection_reasons(self) -> list[RejectionReason]:
        """All rejection reasons across all stages."""
        reasons: list[RejectionReason] = []
        for stage in self.stages:
            reasons.extend(stage.rejection_reasons)
        return reasons

    @property
    def rejection_reason_counts(self) -> dict[RejectionReason, int]:
        """Count of each rejection reason."""
        counts: dict[RejectionReason, int] = {}
        for reason in self.all_rejection_reasons:
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    @property
    def rejection_stage_counts(self) -> dict[PipelineStage, int]:
        """Count of rejections per stage."""
        counts: dict[PipelineStage, int] = {}
        for stage in self.stages:
            if stage.rejection_count > 0:
                counts[stage.stage] = stage.rejection_count
        return counts

    def _get_stage(self, stage: PipelineStage) -> StageResult | None:
        """Get a specific stage result."""
        for s in self.stages:
            if s.stage == stage:
                return s
        return None

    def add_stage(self, result: StageResult) -> None:
        """Add a stage result to the funnel."""
        self.stages.append(result)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for logging/storage."""
        return {
            "task_id": self.task_id,
            "worker_role": self.worker_role,
            "termination_reason": self.termination_reason.value,
            "termination_stage": self.termination_stage.value if self.termination_stage else None,
            "total_candidates": self.total_candidates,
            "accepted_events": self.accepted_events,
            "rejected_events": self.rejected_events,
            "rejection_reason_counts": {
                reason.value: count
                for reason, count in self.rejection_reason_counts.items()
            },
            "rejection_stage_counts": {
                stage.value: count
                for stage, count in self.rejection_stage_counts.items()
            },
            "stages": [
                {
                    "stage": s.stage.value,
                    "passed": s.passed,
                    "items_in": s.items_in,
                    "items_out": s.items_out,
                    "rejection_count": s.rejection_count,
                    "rejection_reasons": [r.value for r in s.rejection_reasons],
                    "details": s.details,
                }
                for s in self.stages
            ],
        }


@dataclass
class DiagnosticsProjection:
    """
    Complete diagnostics for a task execution attempt.

    This captures all diagnostic information needed to understand
    what happened during task execution.

    Invariant: Every task execution MUST produce a fully inspectable diagnostics projection.
    """
    task_id: str
    """ID of the task."""

    # Raw output diagnostics
    raw_output_present: bool = False
    """Was raw output received from backend?"""

    raw_output_length: int | None = None
    """Length of raw output if present."""

    raw_output_digest: str | None = None
    """Hash of raw output for correlation."""

    # Parse stage diagnostics
    json_parse_ok: bool = False
    """Was raw output valid JSON?"""

    json_parse_error: str | None = None
    """JSON parse error message if failed."""

    # Schema stage diagnostics
    schema_ok: bool = False
    """Did parsed JSON match expected schema?"""

    schema_errors: list[str] = field(default_factory=list)
    """Schema validation errors."""

    # Candidate extraction diagnostics
    candidate_count: int = 0
    """Number of candidates extracted."""

    empty_candidate_array: bool = False
    """Was candidate_events array empty?"""

    candidate_missing: bool = False
    """Was candidate_events field missing?"""

    # Policy stage diagnostics
    policy_checked: bool = False
    """Were candidates checked against policy?"""

    policy_rejected_count: int = 0
    """Number of candidates rejected by policy."""

    # Outcome stage diagnostics
    accepted_count: int = 0
    """Number of events accepted."""

    rejected_count: int = 0
    """Number of events rejected."""

    # Rejection classification
    rejection_stage: RejectionStage | None = None
    """Stage where rejection occurred."""

    rejection_reasons: list[RejectionReason] = field(default_factory=list)
    """All rejection reasons."""

    # Provider isolation (from STEP 2)
    is_provider_issue: bool = False
    """Is this a provider issue (not task failure)?"""

    # Acceptance funnel (complete pipeline view)
    acceptance_funnel: AcceptanceFunnel | None = None
    """Complete acceptance funnel."""

    # Metadata
    worker_role: str | None = None
    """Worker role that was executed."""

    backend_type: str | None = None
    """Backend that was used."""

    duration_seconds: float | None = None
    """Duration of task execution."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""

    @property
    def has_rejection_reasons(self) -> bool:
        """Check if there are any rejection reasons."""
        return bool(self.rejection_reasons)

    @property
    def explanation_available(self) -> bool:
        """
        Check if an explanation is available for the outcome.

        If no events were accepted, there MUST be an explanation chain.
        """
        if self.accepted_count > 0:
            return True  # Success needs no explanation
        return self.has_rejection_reasons

    @property
    def primary_rejection_reason(self) -> RejectionReason | None:
        """Get the primary (first) rejection reason."""
        if self.rejection_reasons:
            return self.rejection_reasons[0]
        return None

    @property
    def primary_rejection_stage(self) -> RejectionStage | None:
        """Get the primary rejection stage."""
        return self.rejection_stage

    def get_explanation(self) -> str:
        """
        Get a human-readable explanation of what happened.

        This replaces the opaque "no accepted candidate events" message.
        """
        if self.accepted_count > 0:
            return f"Completed with {self.accepted_count} accepted events."

        if self.is_provider_issue:
            return "Provider issue prevented task completion."

        if not self.raw_output_present:
            return "No output received from backend."

        if not self.json_parse_ok:
            return f"Output was not valid JSON: {self.json_parse_error}"

        if self.candidate_missing:
            return "Output did not contain required 'candidate_events' field."

        if self.empty_candidate_array:
            return "Worker produced empty candidate_events array (no findings to report)."

        if not self.schema_ok:
            errors = "; ".join(self.schema_errors[:3])
            return f"Output schema validation failed: {errors}"

        if self.policy_rejected_count > 0:
            return f"All {self.policy_rejected_count} candidates were rejected by policy."

        if self.rejection_reasons:
            reasons = ", ".join(r.value for r in self.rejection_reasons[:3])
            return f"Candidates rejected: {reasons}"

        return "No accepted candidate events (unknown reason)."

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for logging/storage."""
        return {
            "task_id": self.task_id,
            "raw_output_present": self.raw_output_present,
            "raw_output_length": self.raw_output_length,
            "json_parse_ok": self.json_parse_ok,
            "json_parse_error": self.json_parse_error,
            "schema_ok": self.schema_ok,
            "schema_errors": self.schema_errors,
            "candidate_count": self.candidate_count,
            "empty_candidate_array": self.empty_candidate_array,
            "candidate_missing": self.candidate_missing,
            "policy_checked": self.policy_checked,
            "policy_rejected_count": self.policy_rejected_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "rejection_stage": self.rejection_stage.value if self.rejection_stage else None,
            "rejection_reasons": [r.value for r in self.rejection_reasons],
            "is_provider_issue": self.is_provider_issue,
            "explanation_available": self.explanation_available,
            "explanation": self.get_explanation(),
            "worker_role": self.worker_role,
            "backend_type": self.backend_type,
            "duration_seconds": self.duration_seconds,
            "acceptance_funnel": self.acceptance_funnel.to_dict() if self.acceptance_funnel else None,
            "metadata": self.metadata,
        }


# =============================================================================
# Diagnostics Builder
# =============================================================================

class DiagnosticsBuilder:
    """
    Builder for creating DiagnosticsProjection instances.

    This provides a fluent interface for building up diagnostics
    as the pipeline progresses.
    """

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self._funnel = AcceptanceFunnel(task_id=task_id)
        self._diagnostics = DiagnosticsProjection(task_id=task_id)

    def with_worker_role(self, role: str) -> "DiagnosticsBuilder":
        """Set the worker role."""
        self._diagnostics.worker_role = role
        self._funnel.worker_role = role
        return self

    def with_backend_type(self, backend_type: str) -> "DiagnosticsBuilder":
        """Set the backend type."""
        self._diagnostics.backend_type = backend_type
        return self

    def with_raw_output(self, raw_output: str | None) -> "DiagnosticsBuilder":
        """Record raw output diagnostics."""
        import hashlib

        self._diagnostics.raw_output_present = raw_output is not None and len(raw_output) > 0

        if raw_output:
            self._diagnostics.raw_output_length = len(raw_output)
            self._diagnostics.raw_output_digest = hashlib.sha256(
                raw_output.encode("utf-8")
            ).hexdigest()[:16]

        return self

    def with_json_parse_result(
        self,
        success: bool,
        error: str | None = None,
    ) -> "DiagnosticsBuilder":
        """Record JSON parse stage result."""
        self._diagnostics.json_parse_ok = success
        self._diagnostics.json_parse_error = error

        stage_result = StageResult(
            stage=PipelineStage.PARSE,
            passed=success,
            items_in=1 if self._diagnostics.raw_output_present else 0,
            items_out=1 if success else 0,
        )

        if not success:
            stage_result.rejection_reasons = [RejectionReason.PARSE_NON_JSON]
            self._diagnostics.rejection_reasons.append(RejectionReason.PARSE_NON_JSON)
            self._diagnostics.rejection_stage = RejectionStage.PARSE
            self._funnel.termination_reason = TerminationReason.PARSE_FAILURE
            self._funnel.termination_stage = PipelineStage.PARSE

        self._funnel.add_stage(stage_result)
        return self

    def with_schema_result(
        self,
        success: bool,
        errors: list[str] | None = None,
    ) -> "DiagnosticsBuilder":
        """Record schema validation stage result."""
        self._diagnostics.schema_ok = success
        self._diagnostics.schema_errors = errors or []

        stage_result = StageResult(
            stage=PipelineStage.SCHEMA,
            passed=success,
            items_in=1 if self._diagnostics.json_parse_ok else 0,
            items_out=1 if success else 0,
        )

        if not success:
            stage_result.rejection_reasons = [RejectionReason.SCHEMA_INVALID]
            self._diagnostics.rejection_reasons.append(RejectionReason.SCHEMA_INVALID)
            self._diagnostics.rejection_stage = RejectionStage.SCHEMA
            self._funnel.termination_reason = TerminationReason.SCHEMA_FAILURE
            self._funnel.termination_stage = PipelineStage.SCHEMA

        self._funnel.add_stage(stage_result)
        return self

    def with_extraction_result(
        self,
        candidate_count: int,
        *,
        candidates_missing: bool = False,
        candidates_empty: bool = False,
    ) -> "DiagnosticsBuilder":
        """Record candidate extraction stage result."""
        self._diagnostics.candidate_count = candidate_count
        self._diagnostics.candidate_missing = candidates_missing
        self._diagnostics.empty_candidate_array = candidates_empty

        passed = candidate_count > 0

        stage_result = StageResult(
            stage=PipelineStage.EXTRACT,
            passed=passed,
            items_in=1 if self._diagnostics.schema_ok else 0,
            items_out=candidate_count,
            details={
                "candidate_count": candidate_count,
                "candidates_missing": candidates_missing,
                "candidates_empty": candidates_empty,
            },
        )

        if candidates_missing:
            stage_result.rejection_reasons = [RejectionReason.CANDIDATE_MISSING]
            self._diagnostics.rejection_reasons.append(RejectionReason.CANDIDATE_MISSING)
            self._diagnostics.rejection_stage = RejectionStage.CANDIDATE
            self._funnel.termination_reason = TerminationReason.EXTRACTION_FAILURE
            self._funnel.termination_stage = PipelineStage.EXTRACT
        elif candidates_empty:
            stage_result.rejection_reasons = [RejectionReason.CANDIDATE_EMPTY]
            self._diagnostics.rejection_reasons.append(RejectionReason.CANDIDATE_EMPTY)
            self._diagnostics.rejection_stage = RejectionStage.CANDIDATE
            self._funnel.termination_reason = TerminationReason.NO_CANDIDATES
            self._funnel.termination_stage = PipelineStage.EXTRACT

        self._funnel.add_stage(stage_result)
        return self

    def with_policy_result(
        self,
        accepted_count: int,
        rejected_count: int,
        rejection_reasons: list[RejectionReason] | None = None,
    ) -> "DiagnosticsBuilder":
        """Record policy validation stage result."""
        self._diagnostics.policy_checked = True
        self._diagnostics.policy_rejected_count = rejected_count

        passed = accepted_count > 0

        stage_result = StageResult(
            stage=PipelineStage.POLICY,
            passed=passed,
            items_in=self._diagnostics.candidate_count,
            items_out=accepted_count,
        )

        if rejection_reasons:
            stage_result.rejection_reasons = rejection_reasons
            self._diagnostics.rejection_reasons.extend(rejection_reasons)
            if not self._diagnostics.rejection_stage:
                self._diagnostics.rejection_stage = RejectionStage.POLICY

        if not passed and rejected_count > 0:
            self._funnel.termination_reason = TerminationReason.POLICY_REJECTION
            self._funnel.termination_stage = PipelineStage.POLICY

        self._funnel.add_stage(stage_result)
        return self

    def with_transport_result(
        self,
        accepted_count: int,
        rejected_count: int,
        rejection_reasons: list[RejectionReason] | None = None,
    ) -> "DiagnosticsBuilder":
        """Record transport validation stage result."""
        self._diagnostics.accepted_count = accepted_count
        self._diagnostics.rejected_count = rejected_count

        passed = accepted_count > 0

        stage_result = StageResult(
            stage=PipelineStage.OUTCOME,
            passed=passed,
            items_in=self._diagnostics.candidate_count,
            items_out=accepted_count,
        )

        if rejection_reasons:
            stage_result.rejection_reasons = rejection_reasons
            self._diagnostics.rejection_reasons.extend(rejection_reasons)
            if not self._diagnostics.rejection_stage:
                self._diagnostics.rejection_stage = RejectionStage.TRANSPORT

        if not passed and rejected_count > 0:
            if RejectionReason.TRANSPORT_REJECTED in (rejection_reasons or []):
                self._funnel.termination_reason = TerminationReason.TRANSPORT_FAILURE
                self._funnel.termination_stage = PipelineStage.OUTCOME

        self._funnel.add_stage(stage_result)
        return self

    def with_provider_issue(self, is_issue: bool = True) -> "DiagnosticsBuilder":
        """Mark this as a provider issue (from STEP 2)."""
        self._diagnostics.is_provider_issue = is_issue
        return self

    def with_rejection_chain(self, chain: RejectionChain) -> "DiagnosticsBuilder":
        """Import rejection reasons from a RejectionChain."""
        for rejection in chain.rejections:
            self._diagnostics.rejection_reasons.append(rejection.reason)
            if self._diagnostics.rejection_stage is None:
                self._diagnostics.rejection_stage = rejection.stage
        return self

    def with_duration(self, duration_seconds: float) -> "DiagnosticsBuilder":
        """Set the execution duration."""
        self._diagnostics.duration_seconds = duration_seconds
        return self

    def with_metadata(self, metadata: dict[str, Any]) -> "DiagnosticsBuilder":
        """Add metadata."""
        self._diagnostics.metadata.update(metadata)
        return self

    def build(self) -> DiagnosticsProjection:
        """Build the final diagnostics projection."""
        self._diagnostics.acceptance_funnel = self._funnel
        return self._diagnostics


# =============================================================================
# Aggregation Functions
# =============================================================================

@dataclass
class RejectionAggregation:
    """Aggregated rejection statistics across multiple tasks."""
    total_tasks: int = 0
    """Total tasks analyzed."""

    tasks_with_rejections: int = 0
    """Tasks that had at least one rejection."""

    tasks_no_events: int = 0
    """Tasks that produced no accepted events."""

    rejection_reason_counts: dict[RejectionReason, int] = field(default_factory=dict)
    """Count per rejection reason."""

    rejection_stage_counts: dict[RejectionStage, int] = field(default_factory=dict)
    """Count per rejection stage."""

    def add_diagnostics(self, diagnostics: DiagnosticsProjection) -> None:
        """Add diagnostics from a task to the aggregation."""
        self.total_tasks += 1

        if diagnostics.rejected_count > 0 or diagnostics.has_rejection_reasons:
            self.tasks_with_rejections += 1

        if diagnostics.accepted_count == 0:
            self.tasks_no_events += 1

        for reason in diagnostics.rejection_reasons:
            self.rejection_reason_counts[reason] = (
                self.rejection_reason_counts.get(reason, 0) + 1
            )

        if diagnostics.rejection_stage:
            self.rejection_stage_counts[diagnostics.rejection_stage] = (
                self.rejection_stage_counts.get(diagnostics.rejection_stage, 0) + 1
            )

    @property
    def no_events_rate(self) -> float:
        """Rate of tasks producing no events."""
        if self.total_tasks == 0:
            return 0.0
        return self.tasks_no_events / self.total_tasks

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "total_tasks": self.total_tasks,
            "tasks_with_rejections": self.tasks_with_rejections,
            "tasks_no_events": self.tasks_no_events,
            "no_events_rate": round(self.no_events_rate, 4),
            "rejection_reason_counts": {
                reason.value: count
                for reason, count in sorted(
                    self.rejection_reason_counts.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )
            },
            "rejection_stage_counts": {
                stage.value: count
                for stage, count in sorted(
                    self.rejection_stage_counts.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )
            },
        }


def aggregate_diagnostics(
    diagnostics_list: list[DiagnosticsProjection],
) -> RejectionAggregation:
    """
    Aggregate diagnostics across multiple tasks.

    This provides visibility into patterns across the audit run.
    """
    aggregation = RejectionAggregation()
    for diagnostics in diagnostics_list:
        aggregation.add_diagnostics(diagnostics)
    return aggregation
