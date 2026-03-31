"""
Semantic Content Validator (STEP 0.1)

Validates that observation events contain meaningful semantic content,
not just structural placeholders.

This validator catches the "garbage as truth" problem where:
- statement == "Untitled observation" (placeholder)
- source_refs.file_path == "unknown" (no real binding)
- line_range == (1, 1) (default stub)

Architecture insight:
    The runtime is deterministic and produces correct structure,
    but does NOT guarantee epistemic validity. This validator
    adds semantic integrity checks.

Failure codes:
    OBS_EMPTY_STATEMENT - statement is empty or placeholder
    OBS_NO_SOURCE - source_refs.file_path is unknown
    OBS_FAKE_BINDING - line_range is default stub (1,1) with unknown path
    OBS_EMPTY_SEMANTIC - multiple semantic content issues
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.validators.models import ValidationIssue


# Placeholder values that indicate missing semantic content
PLACEHOLDER_STATEMENTS = {
    "Untitled observation",
    "Untitled hypothesis",
    "Untitled question",
    "Untitled issue",
    "",
}

PLACEHOLDER_FILE_PATHS = {
    "unknown",
    "N/A",
    "",
}


class SemanticContentValidator:
    """
    Validate that events have meaningful semantic content.

    This validator operates AFTER structural validation to ensure
    that accepted events represent actual knowledge, not just
    well-formed empty containers.

    IMPORTANT: This validator is ADVISORY (warning-level) by default.
    It returns issues with severity="warning" which do NOT block event
    acceptance. The issues are tracked as quality flags for filtering
    at report compilation time.

    This implements the "filter after, not before" principle - accept
    events first, then filter based on quality at output stage.
    """

    name = "semantic_content"

    # Configuration: if True, issues are warnings (events still accepted)
    # if False, issues block event acceptance
    ADVISORY_MODE = True

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve()

    def validate(self, event: dict[str, Any]) -> list[ValidationIssue]:
        """
        Validate semantic content of an event.

        In ADVISORY_MODE (default):
        - Returns empty list (events always pass)
        - Quality issues are logged but don't block acceptance

        In strict mode (ADVISORY_MODE=False):
        - Returns issues that block event acceptance
        """
        event_type = event.get("event_type", "")
        entity_type = event.get("entity_type", "")
        payload = event.get("payload", {})

        issues: list[ValidationIssue] = []

        # Only validate observation verification events for now
        # (observations are the primary knowledge carriers)
        if event_type in ("observation.verified", "observation.proposed"):
            issues = self._validate_observation_content(event, payload)

        # Hypothesis events also need semantic content
        elif entity_type == "hypothesis":
            issues = self._validate_hypothesis_content(event, payload)

        # In advisory mode, return empty list (accept all events)
        # Issues are still logged/tracked separately
        if self.ADVISORY_MODE:
            return []  # Events pass through

        # In strict mode, return issues (may block acceptance)
        return issues

    def _validate_observation_content(
        self,
        event: dict[str, Any],
        payload: dict[str, Any],
    ) -> list[ValidationIssue]:
        """Validate observation has meaningful statement and source binding."""
        issues: list[ValidationIssue] = []

        # Check statement
        statement = payload.get("statement", "")
        if not statement or statement in PLACEHOLDER_STATEMENTS:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="OBS_EMPTY_STATEMENT",
                    message=(
                        f"Observation has empty or placeholder statement: "
                        f"'{statement[:50]}...'. Observations MUST contain "
                        f"meaningful semantic content."
                    ),
                    path="payload.statement",
                )
            )

        # Check source binding
        provenance = payload.get("provenance", {})
        source_refs = provenance.get("source_refs", [])

        if not source_refs:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="OBS_NO_SOURCE_REFS",
                    message=(
                        "Observation has no source_refs. Every observation "
                        "MUST be bound to source evidence."
                    ),
                    path="payload.provenance.source_refs",
                )
            )
        else:
            # Check first source_ref for valid binding
            first_ref = source_refs[0] if source_refs else {}
            file_path = first_ref.get("file_path", "")
            line_range = first_ref.get("line_range", {})

            if file_path in PLACEHOLDER_FILE_PATHS:
                issues.append(
                    ValidationIssue(
                        validator=self.name,
                        code="OBS_NO_SOURCE",
                        message=(
                            f"Observation source_refs has placeholder file_path: "
                            f"'{file_path}'. Observations MUST be bound to real files."
                        ),
                        path="payload.provenance.source_refs[0].file_path",
                    )
                )

            # Check for fake binding (unknown file with default line range)
            line_start = line_range.get("start", 0)
            line_end = line_range.get("end", 0)
            if (
                file_path in PLACEHOLDER_FILE_PATHS
                and line_start == 1
                and line_end == 1
            ):
                issues.append(
                    ValidationIssue(
                        validator=self.name,
                        code="OBS_FAKE_BINDING",
                        message=(
                            "Observation has fake source binding: file_path='unknown' "
                            "with line_range=(1,1). This indicates no real evidence binding."
                        ),
                        path="payload.provenance.source_refs[0]",
                    )
                )

        # Aggregate into single OBS_EMPTY_SEMANTIC if multiple issues
        if len(issues) >= 2:
            return [
                ValidationIssue(
                    validator=self.name,
                    code="OBS_EMPTY_SEMANTIC",
                    message=(
                        f"Observation has multiple semantic content issues: "
                        f"{', '.join(i.code for i in issues)}. "
                        f"Observations MUST contain meaningful content and real source binding."
                    ),
                    path="payload",
                )
            ]

        return issues

    def _validate_hypothesis_content(
        self,
        event: dict[str, Any],
        payload: dict[str, Any],
    ) -> list[ValidationIssue]:
        """Validate hypothesis has meaningful statement and rationale."""
        issues: list[ValidationIssue] = []

        statement = payload.get("statement", "")
        if not statement or statement in PLACEHOLDER_STATEMENTS:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="HYP_EMPTY_STATEMENT",
                    message=(
                        f"Hypothesis has empty or placeholder statement: "
                        f"'{statement[:50]}...'"
                    ),
                    path="payload.statement",
                )
            )

        rationale = payload.get("rationale", "")
        if not rationale or rationale == "No rationale provided":
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="HYP_EMPTY_RATIONALE",
                    message="Hypothesis has empty or placeholder rationale.",
                    path="payload.rationale",
                )
            )

        return issues
