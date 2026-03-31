"""
Provider Isolation Layer (STEP 2)

This module implements the provider isolation layer that ensures provider failures
do NOT contaminate runtime correctness metrics. Rate limits and provider errors
are classified separately from task execution failures.

Core Invariant:
Provider failures MUST NOT contaminate runtime correctness metrics

Outcome taxonomy:
- completed_with_events: Task produced accepted events
- completed_no_events: Task completed but produced no accepted events (with explanation)
- provider_throttled: Provider rate-limited the request (not a task failure)
- provider_failed: Provider error (not a task failure)
- runtime_failed: Runtime/logic error (task failure)
- policy_rejected: Candidate rejected by policy
- rejected_contract: Contract violation in output
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OutcomeClass(Enum):
    """
    Classification of task execution outcomes.

    Outcomes are divided into:
    - Success: completed_with_events, completed_no_events
    - Provider issues: provider_throttled, provider_failed (NOT task failures)
    - Task failures: runtime_failed, policy_rejected, rejected_contract
    """

    # Success outcomes
    COMPLETED_WITH_EVENTS = "completed_with_events"
    """Task completed and produced one or more accepted events."""

    COMPLETED_NO_EVENTS = "completed_no_events"
    """Task completed but produced no accepted events. Has explanation chain."""

    # Provider issues (NOT task failures - should not affect task success rate)
    PROVIDER_THROTTLED = "provider_throttled"
    """Provider rate-limited the request. Task should be retryable."""

    PROVIDER_FAILED = "provider_failed"
    """Provider error (timeout, service unavailable, etc.). Not a task logic failure."""

    # Task failures (these ARE task failures)
    RUNTIME_FAILED = "runtime_failed"
    """Runtime/logic error in task execution."""

    POLICY_REJECTED = "policy_rejected"
    """Candidate events rejected by policy rules."""

    REJECTED_CONTRACT = "rejected_contract"
    """Output violated structured contract (invalid JSON, missing fields, etc.)."""


class ProviderErrorType(Enum):
    """Classification of provider-level errors."""

    RATE_LIMITED = "rate_limited"
    """Request was rate-limited by the provider."""

    TIMEOUT = "timeout"
    """Request timed out."""

    SERVICE_UNAVAILABLE = "service_unavailable"
    """Provider service is temporarily unavailable."""

    AUTHENTICATION_ERROR = "authentication_error"
    """Authentication/authorization failed."""

    CONTEXT_OVERFLOW = "context_overflow"
    """Context window exceeded (provider limit)."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """API budget/quota exhausted."""

    UNKNOWN = "unknown"
    """Unknown provider error."""


# Keywords that indicate rate limiting in error messages
RATE_LIMIT_KEYWORDS = frozenset([
    "rate limit",
    "ratelimit",
    "rate-limit",
    "429",
    "too many requests",
    "requests per minute",
    "rpm",
    "tokens per minute",
    "tpm",
    "throttl",
    "quota exceeded",
    "usage limit",
    "limit exceeded",
])

# Keywords that indicate provider unavailability
SERVICE_UNAVAILABLE_KEYWORDS = frozenset([
    "service unavailable",
    "503",
    "502",
    "bad gateway",
    "gateway timeout",
    "internal error",
    "internal server error",
    "500",
    "overloaded",
    "capacity",
])

# Keywords that indicate authentication errors
AUTH_ERROR_KEYWORDS = frozenset([
    "authentication",
    "unauthorized",
    "401",
    "403",
    "forbidden",
    "api key",
    "invalid key",
    "expired",
    "permission",
])

# Keywords that indicate context overflow
CONTEXT_OVERFLOW_KEYWORDS = frozenset([
    "context length",
    "context window",
    "token limit",
    "max tokens",
    "too long",
    "context exceeded",
])


@dataclass(frozen=True)
class ProviderErrorClassification:
    """Result of classifying a provider error."""
    error_type: ProviderErrorType
    """Type of provider error."""

    is_retryable: bool
    """Whether the error is retryable."""

    is_provider_issue: bool
    """Whether this is a provider issue (vs task failure)."""

    suggested_delay_seconds: float | None = None
    """Suggested delay before retry (if rate-limited)."""

    message: str | None = None
    """Human-readable message."""

    raw_error: str | None = None
    """Original error string."""


def classify_provider_error(
    error: Exception | str,
    *,
    backend_type: str | None = None,
    status_code: int | None = None,
) -> ProviderErrorClassification:
    """
    Classify a provider error to determine if it's a rate limit,
    service issue, or other provider problem.

    Args:
        error: The exception or error message
        backend_type: Optional backend type for context
        status_code: Optional HTTP status code

    Returns:
        ProviderErrorClassification with details
    """
    error_str = str(error).lower() if error else ""

    # Check for explicit status codes
    if status_code == 429:
        return ProviderErrorClassification(
            error_type=ProviderErrorType.RATE_LIMITED,
            is_retryable=True,
            is_provider_issue=True,
            suggested_delay_seconds=60.0,
            message="Provider rate limit exceeded",
            raw_error=str(error),
        )

    if status_code in (502, 503, 504):
        return ProviderErrorClassification(
            error_type=ProviderErrorType.SERVICE_UNAVAILABLE,
            is_retryable=True,
            is_provider_issue=True,
            suggested_delay_seconds=30.0,
            message="Provider service temporarily unavailable",
            raw_error=str(error),
        )

    if status_code in (401, 403):
        return ProviderErrorClassification(
            error_type=ProviderErrorType.AUTHENTICATION_ERROR,
            is_retryable=False,
            is_provider_issue=True,
            message="Provider authentication failed",
            raw_error=str(error),
        )

    # Check for keywords in error message
    for keyword in RATE_LIMIT_KEYWORDS:
        if keyword in error_str:
            return ProviderErrorClassification(
                error_type=ProviderErrorType.RATE_LIMITED,
                is_retryable=True,
                is_provider_issue=True,
                suggested_delay_seconds=60.0,
                message=f"Provider rate limit detected: {keyword}",
                raw_error=str(error),
            )

    for keyword in SERVICE_UNAVAILABLE_KEYWORDS:
        if keyword in error_str:
            return ProviderErrorClassification(
                error_type=ProviderErrorType.SERVICE_UNAVAILABLE,
                is_retryable=True,
                is_provider_issue=True,
                suggested_delay_seconds=30.0,
                message=f"Provider service issue detected: {keyword}",
                raw_error=str(error),
            )

    for keyword in AUTH_ERROR_KEYWORDS:
        if keyword in error_str:
            return ProviderErrorClassification(
                error_type=ProviderErrorType.AUTHENTICATION_ERROR,
                is_retryable=False,
                is_provider_issue=True,
                message=f"Provider auth issue detected: {keyword}",
                raw_error=str(error),
            )

    for keyword in CONTEXT_OVERFLOW_KEYWORDS:
        if keyword in error_str:
            return ProviderErrorClassification(
                error_type=ProviderErrorType.CONTEXT_OVERFLOW,
                is_retryable=False,
                is_provider_issue=True,
                message=f"Provider context limit exceeded: {keyword}",
                raw_error=str(error),
            )

    # Check for timeout
    if "timeout" in error_str:
        return ProviderErrorClassification(
            error_type=ProviderErrorType.TIMEOUT,
            is_retryable=True,
            is_provider_issue=True,
            suggested_delay_seconds=10.0,
            message="Provider request timed out",
            raw_error=str(error),
        )

    # Default: unknown provider error
    return ProviderErrorClassification(
        error_type=ProviderErrorType.UNKNOWN,
        is_retryable=False,
        is_provider_issue=True,
        message="Unknown provider error",
        raw_error=str(error),
    )


@dataclass
class TaskOutcome:
    """
    Complete outcome of a task execution.

    This captures all information needed to:
    1. Classify the outcome for metrics
    2. Determine if task should be retried
    3. Update audit status correctly
    """
    task_id: str
    """ID of the task."""

    outcome_class: OutcomeClass
    """Classification of the outcome."""

    worker_role: str | None = None
    """Worker role that was executed."""

    backend_type: str | None = None
    """Backend that was used."""

    accepted_events: int = 0
    """Number of accepted events."""

    rejected_events: int = 0
    """Number of rejected events."""

    rejection_chain: dict[str, Any] | None = None
    """Rejection chain for completed_no_events."""

    provider_error: ProviderErrorClassification | None = None
    """Provider error details if provider_throttled or provider_failed."""

    error_message: str | None = None
    """Error message for failures."""

    retryable: bool = False
    """Whether the task can be retried."""

    retry_delay_seconds: float | None = None
    """Suggested delay before retry."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""

    @property
    def is_success(self) -> bool:
        """Check if this is a successful outcome."""
        return self.outcome_class in (
            OutcomeClass.COMPLETED_WITH_EVENTS,
            OutcomeClass.COMPLETED_NO_EVENTS,
        )

    @property
    def is_provider_issue(self) -> bool:
        """Check if this is a provider issue (not a task failure)."""
        return self.outcome_class in (
            OutcomeClass.PROVIDER_THROTTLED,
            OutcomeClass.PROVIDER_FAILED,
        )

    @property
    def is_task_failure(self) -> bool:
        """Check if this is a task failure (not provider issue)."""
        return self.outcome_class in (
            OutcomeClass.RUNTIME_FAILED,
            OutcomeClass.POLICY_REJECTED,
            OutcomeClass.REJECTED_CONTRACT,
        )

    @property
    def should_affect_metrics(self) -> bool:
        """
        Check if this outcome should affect task success metrics.

        Provider issues should NOT affect success rate metrics.
        """
        return not self.is_provider_issue

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for logging/storage."""
        return {
            "task_id": self.task_id,
            "outcome_class": self.outcome_class.value,
            "worker_role": self.worker_role,
            "backend_type": self.backend_type,
            "accepted_events": self.accepted_events,
            "rejected_events": self.rejected_events,
            "is_success": self.is_success,
            "is_provider_issue": self.is_provider_issue,
            "is_task_failure": self.is_task_failure,
            "should_affect_metrics": self.should_affect_metrics,
            "retryable": self.retryable,
            "retry_delay_seconds": self.retry_delay_seconds,
            "error_message": self.error_message,
            "rejection_chain": self.rejection_chain,
            "provider_error": {
                "error_type": self.provider_error.error_type.value,
                "is_retryable": self.provider_error.is_retryable,
                "message": self.provider_error.message,
            } if self.provider_error else None,
            "metadata": self.metadata,
        }


def classify_task_outcome(
    *,
    task_id: str,
    accepted_events: int,
    rejected_events: int,
    error: Exception | str | None = None,
    rejection_chain: dict[str, Any] | None = None,
    worker_role: str | None = None,
    backend_type: str | None = None,
    status_code: int | None = None,
) -> TaskOutcome:
    """
    Classify the outcome of a task execution.

    This is the main entry point for determining what happened
    during task execution.

    Args:
        task_id: ID of the task
        accepted_events: Number of accepted events
        rejected_events: Number of rejected events
        error: Optional error that occurred
        rejection_chain: Rejection chain if no accepted events
        worker_role: Worker role that was executed
        backend_type: Backend that was used
        status_code: Optional HTTP status code from error

    Returns:
        TaskOutcome with complete classification
    """
    # Case 1: No error, accepted events = success
    if error is None and accepted_events > 0:
        return TaskOutcome(
            task_id=task_id,
            outcome_class=OutcomeClass.COMPLETED_WITH_EVENTS,
            worker_role=worker_role,
            backend_type=backend_type,
            accepted_events=accepted_events,
            rejected_events=rejected_events,
        )

    # Case 2: No error, no accepted events = completed_no_events
    if error is None and accepted_events == 0:
        return TaskOutcome(
            task_id=task_id,
            outcome_class=OutcomeClass.COMPLETED_NO_EVENTS,
            worker_role=worker_role,
            backend_type=backend_type,
            accepted_events=0,
            rejected_events=rejected_events,
            rejection_chain=rejection_chain,
        )

    # Case 3: Error occurred - classify it
    if error is not None:
        provider_classification = classify_provider_error(
            error,
            backend_type=backend_type,
            status_code=status_code,
        )

        # If it's a provider issue, classify accordingly
        if provider_classification.is_provider_issue:
            outcome_class = (
                OutcomeClass.PROVIDER_THROTTLED
                if provider_classification.error_type == ProviderErrorType.RATE_LIMITED
                else OutcomeClass.PROVIDER_FAILED
            )
            return TaskOutcome(
                task_id=task_id,
                outcome_class=outcome_class,
                worker_role=worker_role,
                backend_type=backend_type,
                accepted_events=accepted_events,
                rejected_events=rejected_events,
                provider_error=provider_classification,
                error_message=str(error),
                retryable=provider_classification.is_retryable,
                retry_delay_seconds=provider_classification.suggested_delay_seconds,
            )

        # Not a provider issue - it's a task failure
        return TaskOutcome(
            task_id=task_id,
            outcome_class=OutcomeClass.RUNTIME_FAILED,
            worker_role=worker_role,
            backend_type=backend_type,
            accepted_events=accepted_events,
            rejected_events=rejected_events,
            error_message=str(error),
            retryable=False,
        )

    # Should not reach here, but handle gracefully
    return TaskOutcome(
        task_id=task_id,
        outcome_class=OutcomeClass.RUNTIME_FAILED,
        worker_role=worker_role,
        backend_type=backend_type,
        error_message="Unknown outcome",
    )


def is_throttle_error(error: Exception | str) -> bool:
    """
    Quick check if an error is a throttle/rate-limit error.

    Args:
        error: The exception or error message

    Returns:
        True if this is a throttle error
    """
    classification = classify_provider_error(error)
    return classification.error_type == ProviderErrorType.RATE_LIMITED


def should_stop_claiming_on_error(error: Exception | str) -> bool:
    """
    Determine if we should stop claiming new tasks due to this error.

    For rate limits, we should stop claiming to avoid wasting task slots.

    Args:
        error: The exception or error message

    Returns:
        True if we should stop claiming new tasks
    """
    classification = classify_provider_error(error)
    return classification.error_type in (
        ProviderErrorType.RATE_LIMITED,
        ProviderErrorType.BUDGET_EXHAUSTED,
        ProviderErrorType.AUTHENTICATION_ERROR,
    )
