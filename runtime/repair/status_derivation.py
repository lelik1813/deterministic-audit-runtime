"""
Status Derivation Mapping (STEP 5 extension)

Deterministic mapping from event_type to payload.status for state transition events.
This mapping is used by the repair layer to derive missing status fields.

Core Principle:
    Status derivation is a deterministic, non-semantic transformation.
    The status value is implied by the event_type and can be safely derived.
"""

from __future__ import annotations

from typing import Any

# Mapping from event_type to the target status value
# This covers all state-transition events where status is implied by event_type
EVENT_TYPE_TO_STATUS: dict[str, str] = {
    # Observation state transitions
    "observation.proposed": "proposed",
    "observation.verified": "verified",
    "observation.rejected": "rejected",
    # Hypothesis state transitions
    "hypothesis.proposed": "proposed",
    "hypothesis.sent_to_verification": "in_verification",
    "hypothesis.supported": "supported",
    "hypothesis.rejected": "rejected",
    "hypothesis.unresolved_conflict": "unresolved_conflict",
    # Issue state transitions
    "issue.proposed": "proposed",
    "issue.accepted": "accepted",
    "issue.rejected": "rejected",
    "issue.closed": "closed",
    # Question state transitions
    "question.opened": "open",
    "question.answered": "answered",
    "question.closed": "closed",
    # Contradiction state transitions
    "contradiction.registered": "registered",
    "contradiction.resolved": "resolved",
    # Decision state transitions (special case - always "recorded")
    "decision.recorded": "recorded",
    # Candidate state transitions
    "candidate.proposed": "proposed",
    "candidate.routed_to_verify": "routed_to_verify",
    "candidate.rejected": "rejected",
    "candidate.promoted_to_observation": "promoted_to_observation",
}


def derive_status(event_type: str) -> str | None:
    """
    Derive payload.status from event_type.

    Args:
        event_type: The event type (e.g., "observation.verified")

    Returns:
        The derived status value (e.g., "verified"), or None if not mappable

    Example:
        >>> derive_status("observation.verified")
        'verified'
        >>> derive_status("hypothesis.supported")
        'supported'
        >>> derive_status("audit.created")
        None
    """
    return EVENT_TYPE_TO_STATUS.get(event_type)


def get_status_derivation_map() -> dict[str, str]:
    """
    Return a copy of the event_type to status mapping.

    Useful for testing and introspection.
    """
    return dict(EVENT_TYPE_TO_STATUS)
