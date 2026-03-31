"""
Compose Issue Enforcement (STEP 5)

This module implements the structured contract enforcement for compose_issue.

Core Invariant:
Compose_issue MUST produce either valid candidates OR explicit structured rejection

Scope:
- strict JSON schema
- required: candidate_events, event_type
- mandatory explanation on empty

Behavior:
- no silent empty output
- no implicit failure

Artifacts:
- versioned response schema
- structured no-op output
- rejection explanations

DoD:
- 27 empty-output кейсов больше не существуют как класс
- каждый случай имеет explicit reason
- non-JSON виден и классифицирован
- acceptance funnel полностью прозрачен
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.rejection import (
    RejectionReason,
    RejectionStage,
    RejectionDetail,
    RejectionChain,
)


# =============================================================================
# Structured Explanation Reasons (STEP 5)
# =============================================================================

class NoCandidatesReason(Enum):
    """
    Structured reasons why IssueComposer produced no candidates.

    Every empty candidate_events output MUST include one of these reasons.
    This ensures no silent empty output exists.
    """

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """Verified observations do not support an issue finding."""

    EVIDENCE_CLASS_MISMATCH = "evidence_class_mismatch"
    """Observations use evidence classes not allowed for findings."""

    MISSING_REQUIRED_OBSERVATION = "missing_required_observation"
    """Target observation is missing or not in required state."""

    OPEN_QUESTIONS_UNRESOLVED = "open_questions_unresolved"
    """Critical questions remain unanswered, blocking issue composition."""

    CONSTRAINT_VIOLATION = "constraint_violation"
    """Composing an issue would violate worker constraints."""

    NO_VERIFIED_OBSERVATIONS = "no_verified_observations"
    """No verified observations available to support findings."""

    POLICY_PREVENTS_ISSUE = "policy_prevents_issue"
    """Policy rules prevent issuing a finding for this case."""

    OTHER = "other"
    """Other reason (must include detailed explanation)."""


@dataclass(frozen=True)
class StructuredExplanation:
    """
    Structured explanation for empty candidate_events.

    When IssueComposer produces no candidates, this structured explanation
    MUST be included to satisfy the invariant:
    "no silent empty output"
    """
    reason: NoCandidatesReason
    """The primary reason for no candidates."""

    summary: str
    """Human-readable summary of why no issue could be composed."""

    details: dict[str, Any] = field(default_factory=dict)
    """Additional context-specific details."""

    observation_ids_referenced: list[str] = field(default_factory=list)
    """Observations that were considered but insufficient."""

    question_ids_blocking: list[str] = field(default_factory=list)
    """Questions that blocked issue composition."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for JSON output."""
        return {
            "reason": self.reason.value,
            "summary": self.summary,
            "details": self.details,
            "observation_ids_referenced": self.observation_ids_referenced,
            "question_ids_blocking": self.question_ids_blocking,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StructuredExplanation":
        """Deserialize from dictionary."""
        reason = NoCandidatesReason(data["reason"])
        return cls(
            reason=reason,
            summary=data["summary"],
            details=data.get("details", {}),
            observation_ids_referenced=data.get("observation_ids_referenced", []),
            question_ids_blocking=data.get("question_ids_blocking", []),
        )


# =============================================================================
# Versioned Response Schema (STEP 5)
# =============================================================================

SCHEMA_VERSION = "1.1.0"
"""Current worker output schema version with structured explanation support."""


@dataclass
class ComposeIssueOutput:
    """
    Structured output contract for IssueComposer.

    Version 1.1.0 adds:
    - explanation: Required when candidate_events is empty
    - explanation.reason: Structured no-candidates reason
    - explanation.summary: Human-readable explanation

    Invariant: Either candidate_events is non-empty OR explanation is present.
    """

    schema_version: str = SCHEMA_VERSION
    """Schema version for output contract."""

    slice_id: str = ""
    """Slice ID from input."""

    worker_role: str = "IssueComposer"
    """Worker role (always IssueComposer)."""

    task_id: str = ""
    """Task ID from input."""

    candidate_events: list[dict[str, Any]] = field(default_factory=list)
    """Candidate events (issue.proposed)."""

    explanation: StructuredExplanation | None = None
    """Required when candidate_events is empty."""

    def __post_init__(self) -> None:
        """Validate the output contract."""
        self._validate_contract()

    def _validate_contract(self) -> None:
        """
        Enforce the core invariant:
        Either candidate_events is non-empty OR explanation is present.
        """
        if not self.candidate_events and self.explanation is None:
            raise ComposeIssueContractViolation(
                "IssueComposer output with empty candidate_events MUST include "
                "a structured explanation. This is a contract violation."
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for JSON output."""
        result = {
            "schema_version": self.schema_version,
            "slice_id": self.slice_id,
            "worker_role": self.worker_role,
            "task_id": self.task_id,
            "candidate_events": self.candidate_events,
        }
        if self.explanation is not None:
            result["explanation"] = self.explanation.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComposeIssueOutput":
        """Deserialize from dictionary."""
        explanation = None
        if "explanation" in data and data["explanation"]:
            explanation = StructuredExplanation.from_dict(data["explanation"])

        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            slice_id=data.get("slice_id", ""),
            worker_role=data.get("worker_role", "IssueComposer"),
            task_id=data.get("task_id", ""),
            candidate_events=data.get("candidate_events", []),
            explanation=explanation,
        )


# =============================================================================
# Contract Enforcement
# =============================================================================

class ComposeIssueContractViolation(Exception):
    """Raised when the compose_issue contract is violated."""

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        slice_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.slice_id = slice_id
        self.details = details or {}


def enforce_compose_issue_contract(output: dict[str, Any]) -> ComposeIssueOutput:
    """
    Enforce the compose_issue contract on worker output.

    This function validates that:
    1. Output is valid JSON with required fields
    2. Either candidate_events is non-empty OR explanation is present
    3. explanation has required structure when present

    Args:
        output: Raw output dictionary from IssueComposer

    Returns:
        Validated ComposeIssueOutput

    Raises:
        ComposeIssueContractViolation: If contract is violated
    """
    # Check required fields
    required_fields = ["schema_version", "slice_id", "worker_role", "task_id", "candidate_events"]
    missing = [f for f in required_fields if f not in output]
    if missing:
        raise ComposeIssueContractViolation(
            f"IssueComposer output missing required fields: {', '.join(missing)}",
            task_id=output.get("task_id"),
            slice_id=output.get("slice_id"),
            details={"missing_fields": missing},
        )

    # Validate worker_role
    if output.get("worker_role") != "IssueComposer":
        raise ComposeIssueContractViolation(
            f"Expected worker_role 'IssueComposer', got '{output.get('worker_role')}'",
            task_id=output.get("task_id"),
            slice_id=output.get("slice_id"),
        )

    # Validate candidate_events is a list
    candidate_events = output.get("candidate_events")
    if not isinstance(candidate_events, list):
        raise ComposeIssueContractViolation(
            f"candidate_events must be a list, got {type(candidate_events).__name__}",
            task_id=output.get("task_id"),
            slice_id=output.get("slice_id"),
        )

    # Enforce core invariant: empty candidates require explanation
    if len(candidate_events) == 0:
        if "explanation" not in output or output["explanation"] is None:
            raise ComposeIssueContractViolation(
                "IssueComposer output with empty candidate_events MUST include "
                "a structured explanation. No silent empty output allowed.",
                task_id=output.get("task_id"),
                slice_id=output.get("slice_id"),
                details={"candidate_count": 0, "explanation_present": False},
            )

        # Validate explanation structure
        explanation_data = output["explanation"]
        if not isinstance(explanation_data, dict):
            raise ComposeIssueContractViolation(
                f"explanation must be an object, got {type(explanation_data).__name__}",
                task_id=output.get("task_id"),
                slice_id=output.get("slice_id"),
            )

        if "reason" not in explanation_data:
            raise ComposeIssueContractViolation(
                "explanation must include 'reason' field with structured no-candidates reason",
                task_id=output.get("task_id"),
                slice_id=output.get("slice_id"),
            )

        if "summary" not in explanation_data:
            raise ComposeIssueContractViolation(
                "explanation must include 'summary' field with human-readable explanation",
                task_id=output.get("task_id"),
                slice_id=output.get("slice_id"),
            )

        # Validate reason is a known NoCandidatesReason
        try:
            NoCandidatesReason(explanation_data["reason"])
        except ValueError:
            valid_reasons = [r.value for r in NoCandidatesReason]
            raise ComposeIssueContractViolation(
                f"Invalid explanation reason: '{explanation_data['reason']}'. "
                f"Valid reasons: {', '.join(valid_reasons)}",
                task_id=output.get("task_id"),
                slice_id=output.get("slice_id"),
                details={"invalid_reason": explanation_data["reason"], "valid_reasons": valid_reasons},
            )

    # Build validated output
    return ComposeIssueOutput.from_dict(output)


def classify_empty_candidates(
    output: dict[str, Any],
) -> RejectionDetail:
    """
    Classify an empty candidate_events output using the structured explanation.

    This converts the structured explanation into a RejectionDetail
    that can be added to the rejection chain.

    Args:
        output: Validated IssueComposer output with empty candidates

    Returns:
        RejectionDetail with classified reason
    """
    explanation = output.get("explanation", {})
    reason_str = explanation.get("reason", "other")
    summary = explanation.get("summary", "No candidates produced")

    # Map NoCandidatesReason to (RejectionReason, RejectionStage)
    # Stage MUST match the reason per rejection.py invariant
    reason_mapping = {
        NoCandidatesReason.INSUFFICIENT_EVIDENCE: (
            RejectionReason.POLICY_REJECTED,
            RejectionStage.POLICY,
        ),
        NoCandidatesReason.EVIDENCE_CLASS_MISMATCH: (
            RejectionReason.POLICY_REJECTED,
            RejectionStage.POLICY,
        ),
        NoCandidatesReason.MISSING_REQUIRED_OBSERVATION: (
            RejectionReason.CANDIDATE_MISSING,
            RejectionStage.CANDIDATE,
        ),
        NoCandidatesReason.OPEN_QUESTIONS_UNRESOLVED: (
            RejectionReason.POLICY_REJECTED,
            RejectionStage.POLICY,
        ),
        NoCandidatesReason.CONSTRAINT_VIOLATION: (
            RejectionReason.POLICY_REJECTED,
            RejectionStage.POLICY,
        ),
        NoCandidatesReason.NO_VERIFIED_OBSERVATIONS: (
            RejectionReason.CANDIDATE_EMPTY,
            RejectionStage.CANDIDATE,
        ),
        NoCandidatesReason.POLICY_PREVENTS_ISSUE: (
            RejectionReason.POLICY_REJECTED,
            RejectionStage.POLICY,
        ),
        NoCandidatesReason.OTHER: (
            RejectionReason.POLICY_REJECTED,
            RejectionStage.POLICY,
        ),
    }

    try:
        no_candidates_reason = NoCandidatesReason(reason_str)
    except ValueError:
        no_candidates_reason = NoCandidatesReason.OTHER

    rejection_reason, rejection_stage = reason_mapping.get(
        no_candidates_reason,
        (RejectionReason.POLICY_REJECTED, RejectionStage.POLICY),
    )

    return RejectionDetail(
        reason=rejection_reason,
        stage=rejection_stage,
        message=f"IssueComposer: {summary}",
        metadata={
            "no_candidates_reason": reason_str,
            "observation_ids_referenced": explanation.get("observation_ids_referenced", []),
            "question_ids_blocking": explanation.get("question_ids_blocking", []),
            "explanation_details": explanation.get("details", {}),
        },
    )


# =============================================================================
# Factory Functions for Common Cases
# =============================================================================

def create_insufficient_evidence_explanation(
    observation_id: str,
    evidence_class: str,
    required_classes: list[str],
) -> StructuredExplanation:
    """Create explanation for insufficient evidence case."""
    return StructuredExplanation(
        reason=NoCandidatesReason.INSUFFICIENT_EVIDENCE,
        summary=f"Observation '{observation_id}' uses evidence_class '{evidence_class}' "
                f"which is not sufficient for issue composition.",
        details={
            "evidence_class": evidence_class,
            "required_classes": required_classes,
        },
        observation_ids_referenced=[observation_id],
    )


def create_no_verified_observations_explanation(
    target_observation_id: str,
) -> StructuredExplanation:
    """Create explanation for no verified observations case."""
    return StructuredExplanation(
        reason=NoCandidatesReason.NO_VERIFIED_OBSERVATIONS,
        summary=f"Target observation '{target_observation_id}' is not in verified state.",
        observation_ids_referenced=[target_observation_id],
    )


def create_open_questions_explanation(
    question_ids: list[str],
    questions_summary: str,
) -> StructuredExplanation:
    """Create explanation for unresolved questions case."""
    return StructuredExplanation(
        reason=NoCandidatesReason.OPEN_QUESTIONS_UNRESOLVED,
        summary=f"Cannot compose issue: {questions_summary}",
        question_ids_blocking=question_ids,
    )


def create_other_explanation(
    summary: str,
    details: dict[str, Any] | None = None,
) -> StructuredExplanation:
    """Create explanation for other cases."""
    return StructuredExplanation(
        reason=NoCandidatesReason.OTHER,
        summary=summary,
        details=details or {},
    )
