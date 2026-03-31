"""
Entity Type Derivation Mapping (STEP 5)

Provides deterministic mapping from event_type to entity_type.
This is the ONLY derivation that deterministic repair may perform.

From: repairability_boundary.md §3.4
"""

from __future__ import annotations

# Mapping from event_type to derived entity_type
# This mapping is derived from the canonical event taxonomy
EVENT_TYPE_TO_ENTITY_TYPE: dict[str, str] = {
    # Observation events
    "observation.proposed": "observation",
    "observation.verified": "observation",
    "observation.rejected": "observation",
    # Hypothesis events
    "hypothesis.proposed": "hypothesis",
    "hypothesis.closed": "hypothesis",
    "hypothesis.rejected": "hypothesis",
    # Issue events
    "issue.proposed": "issue",
    "issue.accepted": "issue",
    "issue.rejected": "issue",
    "issue.closed": "issue",
    # Question events
    "question.opened": "question",
    "question.answered": "question",
    "question.closed": "question",
    # Contradiction events
    "contradiction.registered": "contradiction",
    "contradiction.resolved": "contradiction",
    # Candidate events
    "candidate.proposed": "candidate",
    "candidate.routed_to_verify": "candidate",
    "candidate.rejected": "candidate",
    "candidate.promoted_to_observation": "candidate",
}


def derive_entity_type(event_type: str | None) -> str | None:
    """
    Derive entity_type from event_type using deterministic mapping.

    This function implements the ENTITY_TYPE_DERIVATION repair type.
    It returns None if the event_type is unknown or None.

    Args:
        event_type: The event type string (e.g., "observation.proposed")

    Returns:
        The derived entity type (e.g., "observation") or None if unknown

    Examples:
        >>> derive_entity_type("observation.proposed")
        'observation'
        >>> derive_entity_type("issue.accepted")
        'issue'
        >>> derive_entity_type("unknown.event")
        None
        >>> derive_entity_type(None)
        None
    """
    if event_type is None:
        return None
    return EVENT_TYPE_TO_ENTITY_TYPE.get(event_type)
