"""
Rejection Classification Layer (STEP 0)

This module implements the foundational rejection classification layer for the
inner_agent_state runtime. Every rejected candidate MUST have a classified
rejection_reason, and no terminal no-output state can exist without a reason chain.

Pipeline stages:
    parse → schema → candidate → policy → transport

Minimal rejection_reason enum:
    - parse_non_json
    - schema_invalid
    - candidate_missing
    - candidate_empty
    - policy_rejected
    - transport_rejected

INVARIANTS:
    - Every rejected candidate MUST have a classified rejection_reason
    - No terminal no-output state without a reason chain
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RejectionReason(Enum):
    """
    Classification of candidate rejection reasons.

    These are the minimal required reasons covering all pipeline stages.
    Each reason maps to a specific pipeline stage where rejection occurred.
    """

    # Stage: parse - Output could not be parsed as valid JSON
    PARSE_NON_JSON = "parse_non_json"
    """Raw output is not valid JSON. Check model output formatting."""

    # Stage: schema - Parsed JSON does not match expected schema
    SCHEMA_INVALID = "schema_invalid"
    """Parsed JSON violates schema constraints. Missing or invalid fields."""

    # Stage: candidate - No candidate_events array in output
    CANDIDATE_MISSING = "candidate_missing"
    """Output lacks required candidate_events array."""

    # Stage: candidate - candidate_events array is empty
    CANDIDATE_EMPTY = "candidate_empty"
    """candidate_events array exists but contains no entries."""

    # Stage: policy - Candidate rejected by policy rules
    POLICY_REJECTED = "policy_rejected"
    """Candidate violates policy constraints (confidence, evidence class, etc.)."""

    # Stage: transport - Candidate rejected during transport validation
    TRANSPORT_REJECTED = "transport_rejected"
    """Candidate fails transport-level validation (line_start, evidence binding, etc.)."""


class RejectionStage(Enum):
    """Pipeline stage where rejection occurred."""

    PARSE = "parse"
    SCHEMA = "schema"
    CANDIDATE = "candidate"
    POLICY = "policy"
    TRANSPORT = "transport"


# Mapping from rejection reason to stage
REJECTION_REASON_TO_STAGE: dict[RejectionReason, RejectionStage] = {
    RejectionReason.PARSE_NON_JSON: RejectionStage.PARSE,
    RejectionReason.SCHEMA_INVALID: RejectionStage.SCHEMA,
    RejectionReason.CANDIDATE_MISSING: RejectionStage.CANDIDATE,
    RejectionReason.CANDIDATE_EMPTY: RejectionStage.CANDIDATE,
    RejectionReason.POLICY_REJECTED: RejectionStage.POLICY,
    RejectionReason.TRANSPORT_REJECTED: RejectionStage.TRANSPORT,
}


@dataclass(frozen=True)
class RejectionDetail:
    """
    Complete rejection information for a candidate.

    Every rejection MUST include:
    - reason: classified rejection_reason enum value
    - stage: pipeline stage where rejection occurred
    - message: human-readable explanation
    """
    reason: RejectionReason
    """Classified rejection reason."""

    stage: RejectionStage
    """Pipeline stage where rejection occurred."""

    message: str
    """Human-readable explanation of the rejection."""

    raw_output_excerpt: str | None = None
    """Optional excerpt of raw output that caused rejection (for debugging)."""

    schema_path: str | None = None
    """Optional JSON path to the problematic field (for schema errors)."""

    validator_code: str | None = None
    """Optional validator-specific error code."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional context-specific metadata."""

    def __post_init__(self) -> None:
        """Validate that stage matches reason."""
        expected_stage = REJECTION_REASON_TO_STAGE.get(self.reason)
        if expected_stage and self.stage != expected_stage:
            raise ValueError(
                f"RejectionReason.{self.reason.name} must map to "
                f"RejectionStage.{expected_stage.name}, got {self.stage.name}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for logging/storage."""
        return {
            "reason": self.reason.value,
            "stage": self.stage.value,
            "message": self.message,
            "raw_output_excerpt": self.raw_output_excerpt,
            "schema_path": self.schema_path,
            "validator_code": self.validator_code,
            "metadata": self.metadata,
        }


@dataclass
class RejectionChain:
    """
    Chain of rejection reasons leading to terminal no-output state.

    When a task produces no accepted candidates, this chain provides
    a complete explanation of why each candidate was rejected.
    """
    task_id: str
    """ID of the task that produced no accepted candidates."""

    worker_role: str
    """Worker role that was executed."""

    total_candidates: int = 0
    """Total number of candidates attempted."""

    rejections: list[RejectionDetail] = field(default_factory=list)
    """List of rejection details for each rejected candidate."""

    parse_failure: RejectionDetail | None = None
    """If output couldn't be parsed at all, this is the parse failure."""

    def add_rejection(self, detail: RejectionDetail) -> None:
        """Add a rejection to the chain."""
        self.rejections.append(detail)
        self.total_candidates += 1

    def set_parse_failure(self, detail: RejectionDetail) -> None:
        """Set the parse failure (output couldn't be parsed at all)."""
        if detail.reason != RejectionReason.PARSE_NON_JSON:
            raise ValueError("parse_failure must have reason PARSE_NON_JSON")
        self.parse_failure = detail

    @property
    def has_reason_chain(self) -> bool:
        """Check if this chain has any explanation."""
        return bool(self.rejections) or self.parse_failure is not None

    @property
    def stage_summary(self) -> dict[RejectionStage, int]:
        """Count rejections by stage."""
        summary: dict[RejectionStage, int] = {}
        for rejection in self.rejections:
            summary[rejection.stage] = summary.get(rejection.stage, 0) + 1
        return summary

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for logging/storage."""
        return {
            "task_id": self.task_id,
            "worker_role": self.worker_role,
            "total_candidates": self.total_candidates,
            "rejection_count": len(self.rejections),
            "parse_failure": self.parse_failure.to_dict() if self.parse_failure else None,
            "stage_summary": {s.value: c for s, c in self.stage_summary.items()},
            "rejections": [r.to_dict() for r in self.rejections],
        }


# =============================================================================
# Stage-Level Classification Functions
# =============================================================================

def classify_parse_rejection(
    raw_output: str | None,
    error_message: str,
    *,
    output_excerpt_length: int = 200,
) -> RejectionDetail:
    """
    Classify a parse-stage rejection.

    Args:
        raw_output: The raw output that failed to parse (if available)
        error_message: The error message from the parser

    Returns:
        RejectionDetail with PARSE_NON_JSON reason
    """
    excerpt = None
    if raw_output:
        excerpt = raw_output[:output_excerpt_length] if len(raw_output) > output_excerpt_length else raw_output

    return RejectionDetail(
        reason=RejectionReason.PARSE_NON_JSON,
        stage=RejectionStage.PARSE,
        message=f"Failed to parse output as JSON: {error_message}",
        raw_output_excerpt=excerpt,
    )


def classify_schema_rejection(
    schema_path: str | None,
    error_message: str,
    *,
    validator_code: str | None = None,
    raw_output_excerpt: str | None = None,
) -> RejectionDetail:
    """
    Classify a schema-stage rejection.

    Args:
        schema_path: JSON path to the problematic field
        error_message: The error message from schema validation
        validator_code: Optional validator-specific error code
        raw_output_excerpt: Optional excerpt of problematic output

    Returns:
        RejectionDetail with SCHEMA_INVALID reason
    """
    return RejectionDetail(
        reason=RejectionReason.SCHEMA_INVALID,
        stage=RejectionStage.SCHEMA,
        message=error_message,
        schema_path=schema_path,
        validator_code=validator_code,
        raw_output_excerpt=raw_output_excerpt,
    )


def classify_candidate_missing_rejection(
    error_message: str = "Output lacks required candidate_events array.",
) -> RejectionDetail:
    """
    Classify a candidate-stage rejection (missing array).

    Args:
        error_message: The error message

    Returns:
        RejectionDetail with CANDIDATE_MISSING reason
    """
    return RejectionDetail(
        reason=RejectionReason.CANDIDATE_MISSING,
        stage=RejectionStage.CANDIDATE,
        message=error_message,
    )


def classify_candidate_empty_rejection(
    candidate_count: int = 0,
    error_message: str | None = None,
) -> RejectionDetail:
    """
    Classify a candidate-stage rejection (empty array).

    Args:
        candidate_count: Number of candidates (should be 0)
        error_message: Optional custom error message

    Returns:
        RejectionDetail with CANDIDATE_EMPTY reason
    """
    msg = error_message or f"candidate_events array is empty (count={candidate_count})"
    return RejectionDetail(
        reason=RejectionReason.CANDIDATE_EMPTY,
        stage=RejectionStage.CANDIDATE,
        message=msg,
        metadata={"candidate_count": candidate_count},
    )


def classify_policy_rejection(
    error_message: str,
    *,
    validator_code: str | None = None,
    policy_name: str | None = None,
    confidence: float | None = None,
) -> RejectionDetail:
    """
    Classify a policy-stage rejection.

    Args:
        error_message: The error message from policy validation
        validator_code: Optional validator-specific error code
        policy_name: Optional name of the policy that rejected
        confidence: Optional confidence score that was too low

    Returns:
        RejectionDetail with POLICY_REJECTED reason
    """
    metadata: dict[str, Any] = {}
    if policy_name:
        metadata["policy_name"] = policy_name
    if confidence is not None:
        metadata["confidence"] = confidence

    return RejectionDetail(
        reason=RejectionReason.POLICY_REJECTED,
        stage=RejectionStage.POLICY,
        message=error_message,
        validator_code=validator_code,
        metadata=metadata if metadata else None,
    )


def classify_transport_rejection(
    error_message: str,
    *,
    validator_code: str | None = None,
    field_path: str | None = None,
) -> RejectionDetail:
    """
    Classify a transport-stage rejection.

    Args:
        error_message: The error message from transport validation
        validator_code: Optional validator-specific error code
        field_path: Optional path to the problematic field

    Returns:
        RejectionDetail with TRANSPORT_REJECTED reason
    """
    return RejectionDetail(
        reason=RejectionReason.TRANSPORT_REJECTED,
        stage=RejectionStage.TRANSPORT,
        message=error_message,
        validator_code=validator_code,
        schema_path=field_path,
    )


# =============================================================================
# Rejection Event Schema
# =============================================================================

@dataclass(frozen=True)
class RejectionEvent:
    """
    Schema for recording rejection events in the event store.

    This event captures when and why a candidate was rejected,
    providing full traceability for debugging and metrics.
    """
    rejection_reason: RejectionReason
    """Classified rejection reason."""

    rejection_stage: RejectionStage
    """Pipeline stage where rejection occurred."""

    rejection_message: str
    """Human-readable explanation."""

    event_type: str = "candidate.rejected"
    """Event type identifier."""

    candidate_id: str | None = None
    """ID of the rejected candidate (if available)."""

    task_id: str | None = None
    """ID of the task that produced the candidate."""

    worker_role: str | None = None
    """Worker role that was executed."""

    raw_output_digest: str | None = None
    """Hash of raw output (for correlation)."""

    schema_path: str | None = None
    """JSON path to problematic field (for schema errors)."""

    validator_code: str | None = None
    """Validator-specific error code."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional context-specific metadata."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for event storage."""
        return {
            "event_type": self.event_type,
            "rejection_reason": self.rejection_reason.value,
            "rejection_stage": self.rejection_stage.value,
            "rejection_message": self.rejection_message,
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "worker_role": self.worker_role,
            "raw_output_digest": self.raw_output_digest,
            "schema_path": self.schema_path,
            "validator_code": self.validator_code,
            "metadata": self.metadata,
        }


# =============================================================================
# Runtime Invariant Enforcement
# =============================================================================

class RejectionInvariantViolation(Exception):
    """Raised when a rejection invariant is violated."""

    def __init__(self, message: str, *, task_id: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.details = details or {}


def enforce_rejection_invariant(
    *,
    has_accepted: bool,
    rejection_chain: RejectionChain | None,
    task_id: str,
    worker_role: str,
) -> None:
    """
    Enforce the rejection invariant: no terminal no-output state without reason chain.

    This function should be called after processing all candidates.
    If no candidates were accepted, a reason chain MUST exist.

    Args:
        has_accepted: Whether any candidates were accepted
        rejection_chain: The chain of rejections (if any)
        task_id: Task ID for error reporting
        worker_role: Worker role for error reporting

    Raises:
        RejectionInvariantViolation: If no candidates accepted and no reason chain exists
    """
    if has_accepted:
        return  # Invariant satisfied - we have accepted candidates

    if rejection_chain is None or not rejection_chain.has_reason_chain:
        raise RejectionInvariantViolation(
            f"Task '{task_id}' produced no accepted candidates but has no rejection reason chain. "
            "Every completed_no_events state must have a complete explanation.",
            task_id=task_id,
            details={
                "worker_role": worker_role,
                "rejection_chain": rejection_chain.to_dict() if rejection_chain else None,
            },
        )


def create_completed_no_events_explanation(
    rejection_chain: RejectionChain,
) -> dict[str, Any]:
    """
    Create a standardized explanation for completed_no_events state.

    This provides a structured explanation that satisfies the invariant
    that no terminal state exists without a reason chain.

    Args:
        rejection_chain: The chain of rejections

    Returns:
        Dictionary with complete explanation of why no events were accepted
    """
    return {
        "status": "completed_no_events",
        "task_id": rejection_chain.task_id,
        "worker_role": rejection_chain.worker_role,
        "total_candidates": rejection_chain.total_candidates,
        "rejection_count": len(rejection_chain.rejections),
        "parse_failed": rejection_chain.parse_failure is not None,
        "stage_breakdown": {
            stage.value: count
            for stage, count in rejection_chain.stage_summary.items()
        },
        "reason_chain": rejection_chain.to_dict(),
    }
