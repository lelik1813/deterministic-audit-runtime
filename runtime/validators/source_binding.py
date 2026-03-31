from __future__ import annotations

from typing import Any

from runtime.validators.models import ValidationIssue


class SourceBindingValidator:
    """
    Validates source references in events.

    STEP 1 Integration:
    - Accepts pre-normalized evidence with line_range always present
    - Checks for range_inferred flag to warn about degraded evidence
    - Maintains backward compatibility with non-normalized evidence
    """
    name = "source_binding"

    REQUIRED_SOURCE_REF_EVENT_TYPES = {
        "observation.proposed",
        "observation.verified",
        "observation.rejected",
    }

    def validate(self, event: dict[str, Any]) -> list[ValidationIssue]:
        payload = event["payload"]
        event_type = event["event_type"]
        entity_type = event["entity_type"]

        source_refs = self._extract_source_refs(entity_type=entity_type, payload=payload)
        issues: list[ValidationIssue] = []

        if event_type in self.REQUIRED_SOURCE_REF_EVENT_TYPES and not source_refs:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="missing_source_refs",
                    message=f"Event type '{event_type}' requires at least one source reference.",
                    path="payload.provenance.source_refs",
                )
            )
            return issues

        if source_refs and event.get("snapshot_ref") is None:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="missing_event_snapshot_ref",
                    message="Events with source references must include a non-null snapshot_ref.",
                    path="snapshot_ref",
                )
            )

        for index, source_ref in enumerate(source_refs):
            issues.extend(self._validate_source_ref(event=event, source_ref=source_ref, index=index))

        return issues

    def _validate_source_ref(
        self,
        event: dict[str, Any],
        source_ref: dict[str, Any],
        index: int,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        base_path = self._source_ref_path(entity_type=event["entity_type"], index=index)

        # STEP 1: Handle line_range - it should always exist after normalization
        line_range = source_ref.get("line_range")
        if not isinstance(line_range, dict):
            # This should not happen after STEP 1 normalization
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="missing_line_range",
                    message="Source reference must include line_range.",
                    path=f"{base_path}.line_range",
                )
            )
            return issues

        # Check for required line_range fields
        start = line_range.get("start")
        end = line_range.get("end")

        if start is None or end is None:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="incomplete_line_range",
                    message="Source reference line_range must include both start and end.",
                    path=f"{base_path}.line_range",
                )
            )
            return issues

        if not isinstance(start, int) or not isinstance(end, int):
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="invalid_line_range_type",
                    message="Source reference line_range start and end must be integers.",
                    path=f"{base_path}.line_range",
                )
            )
            return issues

        if start > end:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="invalid_line_range",
                    message="Source reference line_range.start must be less than or equal to line_range.end.",
                    path=f"{base_path}.line_range",
                )
            )

        # STEP 1: Check for inferred ranges (degraded evidence)
        if source_ref.get("range_inferred") is True:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="inferred_line_range",
                    message=(
                        f"Source reference has inferred line range ({start}-{end}). "
                        f"Reason: {source_ref.get('normalization_warning', 'unknown')}"
                    ),
                    path=f"{base_path}.line_range",
                )
            )

        column_range = source_ref.get("column_range")
        if column_range is not None and column_range.get("start", 0) > column_range.get("end", 0):
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="invalid_column_range",
                    message=(
                        "Source reference column_range.start must be less than or equal to "
                        "column_range.end."
                    ),
                    path=f"{base_path}.column_range",
                )
            )

        event_snapshot_ref = event.get("snapshot_ref")
        source_snapshot_ref = source_ref.get("snapshot_ref")
        if (
            event_snapshot_ref is not None
            and source_snapshot_ref is not None
            and source_snapshot_ref != event_snapshot_ref
        ):
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="snapshot_ref_mismatch",
                    message=(
                        f"Source reference snapshot_ref '{source_snapshot_ref}' does not match "
                        f"event snapshot_ref '{event_snapshot_ref}'."
                    ),
                    path=f"{base_path}.snapshot_ref",
                )
            )

        return issues

    @staticmethod
    def _extract_source_refs(entity_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if entity_type == "observation":
            provenance = payload.get("provenance")
            if isinstance(provenance, dict):
                return provenance.get("source_refs", [])
            return []
        if entity_type == "hypothesis":
            return payload.get("supporting_source_refs", [])
        if entity_type in {"contradiction", "decision"}:
            return payload.get("source_refs", [])
        return []

    @staticmethod
    def _source_ref_path(entity_type: str, index: int) -> str:
        if entity_type == "observation":
            return f"payload.provenance.source_refs[{index}]"
        if entity_type == "hypothesis":
            return f"payload.supporting_source_refs[{index}]"
        return f"payload.source_refs[{index}]"
