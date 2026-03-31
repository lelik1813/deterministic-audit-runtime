"""
Unit tests for Provider Isolation Layer (STEP 2)

These tests verify that:
1. Rate limits are classified as provider_throttled
2. Provider errors don't count as task failures
3. Task failures are correctly distinguished from provider issues
4. should_affect_metrics correctly identifies what to count
"""

from __future__ import annotations

import pytest

from runtime.outcome import (
    OutcomeClass,
    ProviderErrorType,
    ProviderErrorClassification,
    TaskOutcome,
    classify_provider_error,
    classify_task_outcome,
    is_throttle_error,
    should_stop_claiming_on_error,
)


class TestOutcomeClassEnum:
    """Tests for OutcomeClass enum completeness."""

    def test_required_outcomes_exist(self) -> None:
        """Verify all required outcomes are defined."""
        required_outcomes = {
            "completed_with_events",
            "completed_no_events",
            "provider_throttled",
            "provider_failed",
            "runtime_failed",
            "policy_rejected",
            "rejected_contract",
        }
        actual_outcomes = {outcome.value for outcome in OutcomeClass}
        assert required_outcomes <= actual_outcomes, (
            f"Missing required outcomes: {required_outcomes - actual_outcomes}"
        )


class TestProviderErrorType:
    """Tests for ProviderErrorType enum."""

    def test_required_error_types_exist(self) -> None:
        """Verify all required error types are defined."""
        required_types = {
            "rate_limited",
            "timeout",
            "service_unavailable",
            "authentication_error",
            "context_overflow",
            "budget_exhausted",
            "unknown",
        }
        actual_types = {t.value for t in ProviderErrorType}
        assert required_types <= actual_types


class TestClassifyProviderError:
    """Tests for classify_provider_error function."""

    def test_rate_limit_429(self) -> None:
        """Verify 429 status code is classified as rate limited."""
        result = classify_provider_error("Error", status_code=429)
        assert result.error_type == ProviderErrorType.RATE_LIMITED
        assert result.is_retryable is True
        assert result.is_provider_issue is True
        assert result.suggested_delay_seconds is not None

    def test_rate_limit_keyword(self) -> None:
        """Verify rate limit keywords are detected."""
        result = classify_provider_error("Rate limit exceeded, please retry")
        assert result.error_type == ProviderErrorType.RATE_LIMITED
        assert result.is_retryable is True

    def test_rate_limit_429_in_message(self) -> None:
        """Verify 429 keyword in message is detected."""
        result = classify_provider_error("HTTP 429 Too Many Requests")
        assert result.error_type == ProviderErrorType.RATE_LIMITED

    def test_service_unavailable_503(self) -> None:
        """Verify 503 status code is classified as service unavailable."""
        result = classify_provider_error("Error", status_code=503)
        assert result.error_type == ProviderErrorType.SERVICE_UNAVAILABLE
        assert result.is_retryable is True
        assert result.is_provider_issue is True

    def test_service_unavailable_502(self) -> None:
        """Verify 502 status code is classified as service unavailable."""
        result = classify_provider_error("Error", status_code=502)
        assert result.error_type == ProviderErrorType.SERVICE_UNAVAILABLE

    def test_auth_error_401(self) -> None:
        """Verify 401 status code is classified as auth error."""
        result = classify_provider_error("Error", status_code=401)
        assert result.error_type == ProviderErrorType.AUTHENTICATION_ERROR
        assert result.is_retryable is False  # Auth errors not retryable

    def test_auth_error_keyword(self) -> None:
        """Verify auth error keywords are detected."""
        result = classify_provider_error("Authentication failed: invalid API key")
        assert result.error_type == ProviderErrorType.AUTHENTICATION_ERROR

    def test_timeout_keyword(self) -> None:
        """Verify timeout keyword is detected."""
        result = classify_provider_error("Request timeout after 30s")
        assert result.error_type == ProviderErrorType.TIMEOUT
        assert result.is_retryable is True

    def test_context_overflow_keyword(self) -> None:
        """Verify context overflow keywords are detected."""
        result = classify_provider_error("Context length exceeded max tokens")
        assert result.error_type == ProviderErrorType.CONTEXT_OVERFLOW
        assert result.is_provider_issue is True

    def test_unknown_error(self) -> None:
        """Verify unknown errors are handled."""
        result = classify_provider_error("Something weird happened")
        assert result.error_type == ProviderErrorType.UNKNOWN
        assert result.is_provider_issue is True

    def test_none_error(self) -> None:
        """Verify None error is handled."""
        result = classify_provider_error(None)
        assert result.error_type == ProviderErrorType.UNKNOWN


class TestTaskOutcome:
    """Tests for TaskOutcome dataclass."""

    def test_completed_with_events_is_success(self) -> None:
        """Verify completed_with_events is marked as success."""
        outcome = TaskOutcome(
            task_id="task_1",
            outcome_class=OutcomeClass.COMPLETED_WITH_EVENTS,
            accepted_events=5,
            rejected_events=0,
        )
        assert outcome.is_success is True
        assert outcome.is_provider_issue is False
        assert outcome.is_task_failure is False
        assert outcome.should_affect_metrics is True

    def test_completed_no_events_is_success(self) -> None:
        """Verify completed_no_events is still marked as success."""
        outcome = TaskOutcome(
            task_id="task_1",
            outcome_class=OutcomeClass.COMPLETED_NO_EVENTS,
            accepted_events=0,
            rejected_events=3,
            rejection_chain={"reasons": ["policy_rejected"]},
        )
        assert outcome.is_success is True
        assert outcome.is_provider_issue is False
        assert outcome.should_affect_metrics is True

    def test_provider_throttled_is_not_task_failure(self) -> None:
        """Verify provider_throttled is NOT a task failure."""
        provider_err = ProviderErrorClassification(
            error_type=ProviderErrorType.RATE_LIMITED,
            is_retryable=True,
            is_provider_issue=True,
            suggested_delay_seconds=60.0,
        )
        outcome = TaskOutcome(
            task_id="task_1",
            outcome_class=OutcomeClass.PROVIDER_THROTTLED,
            provider_error=provider_err,
            retryable=True,
            retry_delay_seconds=60.0,
        )
        assert outcome.is_success is False
        assert outcome.is_provider_issue is True
        assert outcome.is_task_failure is False  # KEY: not a task failure
        assert outcome.should_affect_metrics is False  # KEY: doesn't affect metrics
        assert outcome.retryable is True

    def test_provider_failed_is_not_task_failure(self) -> None:
        """Verify provider_failed is NOT a task failure."""
        provider_err = ProviderErrorClassification(
            error_type=ProviderErrorType.SERVICE_UNAVAILABLE,
            is_retryable=True,
            is_provider_issue=True,
        )
        outcome = TaskOutcome(
            task_id="task_1",
            outcome_class=OutcomeClass.PROVIDER_FAILED,
            provider_error=provider_err,
        )
        assert outcome.is_provider_issue is True
        assert outcome.is_task_failure is False
        assert outcome.should_affect_metrics is False

    def test_runtime_failed_is_task_failure(self) -> None:
        """Verify runtime_failed IS a task failure."""
        outcome = TaskOutcome(
            task_id="task_1",
            outcome_class=OutcomeClass.RUNTIME_FAILED,
            error_message="Internal logic error",
        )
        assert outcome.is_success is False
        assert outcome.is_provider_issue is False
        assert outcome.is_task_failure is True
        assert outcome.should_affect_metrics is True


class TestClassifyTaskOutcome:
    """Tests for classify_task_outcome function."""

    def test_accepted_events_is_completed_with_events(self) -> None:
        """Verify accepted events results in completed_with_events."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=3,
            rejected_events=1,
        )
        assert outcome.outcome_class == OutcomeClass.COMPLETED_WITH_EVENTS
        assert outcome.is_success is True

    def test_no_accepted_events_is_completed_no_events(self) -> None:
        """Verify no accepted events results in completed_no_events."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=0,
            rejected_events=2,
            rejection_chain={"reasons": ["schema_invalid"]},
        )
        assert outcome.outcome_class == OutcomeClass.COMPLETED_NO_EVENTS
        assert outcome.is_success is True

    def test_rate_limit_error_is_provider_throttled(self) -> None:
        """Verify rate limit error results in provider_throttled."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=0,
            rejected_events=0,
            error="Rate limit exceeded",
        )
        assert outcome.outcome_class == OutcomeClass.PROVIDER_THROTTLED
        assert outcome.is_provider_issue is True
        assert outcome.should_affect_metrics is False

    def test_429_status_is_provider_throttled(self) -> None:
        """Verify 429 status code results in provider_throttled."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=0,
            rejected_events=0,
            error="Error",
            status_code=429,
        )
        assert outcome.outcome_class == OutcomeClass.PROVIDER_THROTTLED
        assert outcome.retryable is True

    def test_service_unavailable_is_provider_failed(self) -> None:
        """Verify service unavailable results in provider_failed."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=0,
            rejected_events=0,
            error="Service unavailable",
        )
        assert outcome.outcome_class == OutcomeClass.PROVIDER_FAILED
        assert outcome.is_provider_issue is True
        assert outcome.should_affect_metrics is False

    def test_to_dict_includes_all_fields(self) -> None:
        """Verify serialization includes all relevant fields."""
        outcome = TaskOutcome(
            task_id="task_1",
            outcome_class=OutcomeClass.COMPLETED_WITH_EVENTS,
            worker_role="Reader",
            backend_type="claude_sdk",
            accepted_events=5,
            rejected_events=1,
        )
        d = outcome.to_dict()
        assert d["task_id"] == "task_1"
        assert d["outcome_class"] == "completed_with_events"
        assert d["worker_role"] == "Reader"
        assert d["backend_type"] == "claude_sdk"
        assert d["accepted_events"] == 5
        assert d["is_success"] is True
        assert d["should_affect_metrics"] is True


class TestIsThrottleError:
    """Tests for is_throttle_error function."""

    def test_rate_limit_is_throttle(self) -> None:
        """Verify rate limit is detected as throttle."""
        assert is_throttle_error("Rate limit exceeded") is True

    def test_429_is_throttle(self) -> None:
        """Verify 429 error is detected as throttle."""
        assert is_throttle_error("HTTP 429") is True

    def test_timeout_is_not_throttle(self) -> None:
        """Verify timeout is NOT detected as throttle."""
        assert is_throttle_error("Request timeout") is False

    def test_service_unavailable_is_not_throttle(self) -> None:
        """Verify service unavailable is NOT detected as throttle."""
        assert is_throttle_error("Service unavailable") is False


class TestShouldStopClaimingOnError:
    """Tests for should_stop_claiming_on_error function."""

    def test_stop_on_rate_limit(self) -> None:
        """Verify we stop claiming on rate limit."""
        assert should_stop_claiming_on_error("Rate limit exceeded") is True

    def test_stop_on_auth_error(self) -> None:
        """Verify we stop claiming on auth error."""
        assert should_stop_claiming_on_error("Authentication failed") is True

    def test_continue_on_timeout(self) -> None:
        """Verify we can continue on timeout (transient)."""
        assert should_stop_claiming_on_error("Request timeout") is False

    def test_continue_on_service_unavailable(self) -> None:
        """Verify we can continue on service unavailable (transient)."""
        assert should_stop_claiming_on_error("Service unavailable") is False


class TestStep2DoD:
    """Tests for STEP 2 Definition of Done criteria."""

    def test_rate_limit_not_generic_failure(self) -> None:
        """DoD: rate-limit → provider_throttled (not generic failure)."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=0,
            rejected_events=0,
            error="Rate limit exceeded",
        )
        assert outcome.outcome_class == OutcomeClass.PROVIDER_THROTTLED
        assert outcome.outcome_class != OutcomeClass.RUNTIME_FAILED

    def test_provider_issue_not_counted_in_metrics(self) -> None:
        """DoD: provider issues don't affect benchmark denominator."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=0,
            rejected_events=0,
            error="Rate limit exceeded",
        )
        assert outcome.is_provider_issue is True
        assert outcome.should_affect_metrics is False

    def test_throttle_detected_and_retryable(self) -> None:
        """DoD: audit status correctly reflects throttle with retry info."""
        outcome = classify_task_outcome(
            task_id="task_1",
            accepted_events=0,
            rejected_events=0,
            error="Rate limit exceeded",
            status_code=429,
        )
        assert outcome.outcome_class == OutcomeClass.PROVIDER_THROTTLED
        assert outcome.retryable is True
        assert outcome.retry_delay_seconds is not None
        assert outcome.retry_delay_seconds > 0

    def test_task_failure_still_affects_metrics(self) -> None:
        """DoD: legitimate task failures still affect metrics.

        Note: By default, unknown errors that don't match provider patterns
        are still classified as provider issues (safe default).
        Task failures happen at the processing/validation level, not error level.
        """
        # The key test is that is_task_failure cases affect metrics
        outcome = TaskOutcome(
            task_id="task_1",
            outcome_class=OutcomeClass.RUNTIME_FAILED,
            error_message="Internal logic error",
        )
        # Verify it's NOT a provider issue
        assert outcome.is_provider_issue is False
        assert outcome.is_task_failure is True
        assert outcome.should_affect_metrics is True
