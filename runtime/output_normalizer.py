"""
Output Normalizer with Rejection Classification (STEP 0 + STEP 1)

This module normalizes worker output and integrates with the rejection classification layer.
Every rejection is classified with a rejection_reason and captured at the appropriate pipeline stage.

Pipeline stages:
    parse → schema → candidate → policy → transport

STEP 1 Integration:
    - Evidence normalization with schema v1.3
    - All evidence items are transport-valid with explicit typing
    - Missing line_start/line_end handled gracefully with range_inferred flag
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from runtime.evidence import (
    DEFAULT_LINE_END,
    DEFAULT_LINE_START,
    NormalizedSourceRef,
    normalize_source_ref,
)
from runtime.rejection import (
    RejectionDetail,
    RejectionReason,
    RejectionStage,
    classify_candidate_empty_rejection,
    classify_candidate_missing_rejection,
    classify_parse_rejection,
    classify_schema_rejection,
    classify_transport_rejection,
)


@dataclass(frozen=True)
class NormalizedOutput:
    """Result of normalizing worker output with rejection classification."""
    success: bool
    """Whether normalization succeeded."""

    payload: dict[str, Any] | None = None
    """Normalized payload if success."""

    candidate_events: list[dict[str, Any]] | None = None
    """Extracted candidate events if success."""

    rejection: RejectionDetail | None = None
    """Rejection details if normalization failed."""

    raw_output_digest: str | None = None
    """Hash of raw output for correlation."""


class OutputNormalizer:
    """
    Normalizes worker output with proper rejection classification.

    This class implements the parse and schema stages of the pipeline,
    classifying rejections according to the rejection taxonomy.
    """

    def __init__(
        self,
        *,
        output_excerpt_length: int = 200,
    ) -> None:
        self.output_excerpt_length = output_excerpt_length

    def normalize(
        self,
        raw_output: str | None,
        *,
        expected_keys: set[str] | None = None,
    ) -> NormalizedOutput:
        """
        Normalize raw output with rejection classification.

        Pipeline stages covered:
        1. parse: JSON parsing
        2. schema: schema validation

        Args:
            raw_output: Raw output string to normalize
            expected_keys: Optional set of required top-level keys

        Returns:
            NormalizedOutput with success or rejection details
        """
        # Stage 1: Parse
        if raw_output is None:
            return NormalizedOutput(
                success=False,
                rejection=classify_parse_rejection(
                    raw_output=None,
                    error_message="Raw output is None.",
                ),
            )

        raw_output = raw_output.strip()
        if not raw_output:
            return NormalizedOutput(
                success=False,
                rejection=classify_parse_rejection(
                    raw_output=None,
                    error_message="Raw output is empty.",
                ),
            )

        try:
            decoded = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            return NormalizedOutput(
                success=False,
                rejection=classify_parse_rejection(
                    raw_output=raw_output,
                    error_message=f"Invalid JSON: {exc}",
                    output_excerpt_length=self.output_excerpt_length,
                ),
            )

        # Stage 2: Schema
        if not isinstance(decoded, dict):
            return NormalizedOutput(
                success=False,
                rejection=classify_schema_rejection(
                    schema_path=None,
                    error_message="Output must decode to a JSON object.",
                    raw_output_excerpt=raw_output[:self.output_excerpt_length],
                ),
            )

        # Check expected keys if provided
        if expected_keys:
            missing_keys = expected_keys - set(decoded.keys())
            if missing_keys:
                return NormalizedOutput(
                    success=False,
                    rejection=classify_schema_rejection(
                        schema_path=list(missing_keys)[0] if len(missing_keys) == 1 else None,
                        error_message=f"Missing required keys: {', '.join(sorted(missing_keys))}",
                        raw_output_excerpt=raw_output[:self.output_excerpt_length],
                    ),
                )

        # Extract candidate events
        candidate_events = decoded.get("candidate_events")
        if candidate_events is None:
            return NormalizedOutput(
                success=False,
                rejection=classify_candidate_missing_rejection(),
            )

        if not isinstance(candidate_events, list):
            return NormalizedOutput(
                success=False,
                rejection=classify_schema_rejection(
                    schema_path="candidate_events",
                    error_message="candidate_events must be a list.",
                    raw_output_excerpt=raw_output[:self.output_excerpt_length],
                ),
            )

        # Check for empty candidate events (valid but empty)
        if not candidate_events:
            return NormalizedOutput(
                success=False,
                rejection=classify_candidate_empty_rejection(
                    candidate_count=0,
                ),
            )

        # STEP 1: Transport-level evidence normalization
        normalized_events, transport_issues = self._normalize_candidate_events_evidence(
            candidate_events,
            raw_output_excerpt=raw_output[:self.output_excerpt_length],
        )

        if transport_issues:
            # Return first transport issue as rejection
            return NormalizedOutput(
                success=False,
                rejection=transport_issues[0],
            )

        return NormalizedOutput(
            success=True,
            payload=decoded,
            candidate_events=normalized_events,
        )

    def _normalize_candidate_events_evidence(
        self,
        candidate_events: list[dict[str, Any]],
        *,
        raw_output_excerpt: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[RejectionDetail]]:
        """
        Normalize evidence in candidate events (STEP 1 transport stage).

        This method ensures all evidence items are transport-valid by:
        1. Normalizing source_refs to have valid line_range
        2. Marking inferred ranges with range_inferred=True
        3. Adding normalization_warning for degraded evidence

        Args:
            candidate_events: List of candidate events to normalize
            raw_output_excerpt: Excerpt of raw output for error messages

        Returns:
            Tuple of (normalized_events, transport_issues)
        """
        normalized_events: list[dict[str, Any]] = []
        transport_issues: list[RejectionDetail] = []

        for event_index, event in enumerate(candidate_events):
            if not isinstance(event, dict):
                transport_issues.append(
                    classify_transport_rejection(
                        error_message=f"Candidate event at index {event_index} is not an object.",
                        field_path=f"candidate_events[{event_index}]",
                    )
                )
                continue

            normalized_event = dict(event)

            # Normalize source_refs in payload if present
            payload = event.get("payload")
            if isinstance(payload, dict):
                normalized_payload = dict(payload)

                # Handle provenance.source_refs (observation events)
                provenance = payload.get("provenance")
                if isinstance(provenance, dict):
                    source_refs = provenance.get("source_refs")
                    if isinstance(source_refs, list):
                        normalized_refs, issues = self._normalize_source_refs_list(
                            source_refs,
                            base_path=f"candidate_events[{event_index}].payload.provenance.source_refs",
                        )
                        if issues:
                            transport_issues.extend(issues)
                            continue
                        normalized_provenance = dict(provenance)
                        normalized_provenance["source_refs"] = normalized_refs
                        normalized_payload["provenance"] = normalized_provenance

                # Handle evidence field (common pattern)
                evidence = payload.get("evidence")
                if isinstance(evidence, list):
                    normalized_evidence, issues = self._normalize_evidence_list(
                        evidence,
                        base_path=f"candidate_events[{event_index}].payload.evidence",
                    )
                    if issues:
                        transport_issues.extend(issues)
                        continue
                    normalized_payload["evidence"] = normalized_evidence

                normalized_event["payload"] = normalized_payload

            normalized_events.append(normalized_event)

        return normalized_events, transport_issues

    def _normalize_source_refs_list(
        self,
        source_refs: list[Any],
        *,
        base_path: str,
    ) -> tuple[list[dict[str, Any]], list[RejectionDetail]]:
        """
        Normalize a list of source references.

        Args:
            source_refs: List of source refs to normalize
            base_path: Base path for error messages

        Returns:
            Tuple of (normalized_refs, issues)
        """
        normalized_refs: list[dict[str, Any]] = []
        issues: list[RejectionDetail] = []

        for ref_index, source_ref in enumerate(source_refs):
            if not isinstance(source_ref, dict):
                issues.append(
                    classify_transport_rejection(
                        error_message=f"Source ref at index {ref_index} is not an object.",
                        field_path=f"{base_path}[{ref_index}]",
                    )
                )
                continue

            normalized = normalize_source_ref(source_ref)
            if normalized is None:
                issues.append(
                    classify_transport_rejection(
                        error_message=(
                            f"Source ref at index {ref_index} is missing required fields "
                            "(file_path or snapshot_ref)."
                        ),
                        field_path=f"{base_path}[{ref_index}]",
                    )
                )
                continue

            normalized_refs.append(normalized.to_dict())

        return normalized_refs, issues

    def _normalize_evidence_list(
        self,
        evidence_list: list[Any],
        *,
        base_path: str,
    ) -> tuple[list[dict[str, Any]], list[RejectionDetail]]:
        """
        Normalize a list of evidence items.

        Args:
            evidence_list: List of evidence to normalize
            base_path: Base path for error messages

        Returns:
            Tuple of (normalized_evidence, issues)
        """
        normalized_evidence: list[dict[str, Any]] = []
        issues: list[RejectionDetail] = []

        for ev_index, evidence_item in enumerate(evidence_list):
            if not isinstance(evidence_item, dict):
                issues.append(
                    classify_transport_rejection(
                        error_message=f"Evidence item at index {ev_index} is not an object.",
                        field_path=f"{base_path}[{ev_index}]",
                    )
                )
                continue

            normalized_item = dict(evidence_item)

            # Normalize line_range if present
            line_range = evidence_item.get("line_range")
            if isinstance(line_range, dict):
                start = line_range.get("start")
                end = line_range.get("end")

                # Check for missing/invalid values
                if not isinstance(start, int) or start < 1:
                    # Use defaults and mark as inferred
                    normalized_item["line_range"] = {
                        "start": DEFAULT_LINE_START,
                        "end": DEFAULT_LINE_END,
                    }
                    normalized_item["range_inferred"] = True
                    normalized_item["normalization_warning"] = (
                        f"line_range.start missing or invalid, defaulted to {DEFAULT_LINE_START}"
                    )
                elif not isinstance(end, int) or end < 1:
                    normalized_item["line_range"] = {
                        "start": start,
                        "end": start,
                    }
                    normalized_item["range_inferred"] = True
                    normalized_item["normalization_warning"] = (
                        f"line_range.end missing or invalid, defaulted to {start}"
                    )
                else:
                    normalized_item["line_range"] = {"start": start, "end": end}
                    normalized_item["range_inferred"] = False
            else:
                # line_range is missing entirely - create default
                normalized_item["line_range"] = {
                    "start": DEFAULT_LINE_START,
                    "end": DEFAULT_LINE_END,
                }
                normalized_item["range_inferred"] = True
                normalized_item["normalization_warning"] = (
                    "line_range missing, defaulted to 1-1"
                )

            normalized_evidence.append(normalized_item)

        return normalized_evidence, issues


def normalize_with_rejection_chain(
    raw_output: str | None,
    *,
    expected_keys: set[str] | None = None,
    task_id: str = "unknown",
    worker_role: str = "unknown",
) -> tuple[NormalizedOutput, dict[str, Any] | None]:
    """
    Normalize output and return rejection chain data for diagnostics.

    This is a convenience wrapper that produces both the NormalizedOutput
    and a dictionary suitable for rejection chain recording.

    Returns:
        Tuple of (NormalizedOutput, rejection_chain_dict or None)
    """
    normalizer = OutputNormalizer()
    result = normalizer.normalize(raw_output, expected_keys=expected_keys)

    chain_data = None
    if not result.success and result.rejection:
        chain_data = {
            "task_id": task_id,
            "worker_role": worker_role,
            "rejection_reason": result.rejection.reason.value,
            "rejection_stage": result.rejection.stage.value,
            "rejection_message": result.rejection.message,
        }

    return result, chain_data
