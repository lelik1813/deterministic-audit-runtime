"""
Deterministic Repairer Implementation (STEP 5)

Implements the DeterministicRepairer that operates between typed IR construction
and validation, applying ONLY classified repairs and logging all modifications.

Core Principle (from repairability_boundary.md):
    Deterministic repair may ONLY modify fields that are both defaultable AND non-semantic.
    All other modifications require model retry with failure mode classification.

From: deterministic_repair_design.md
Step: STEP 5 — Deterministic Repair Layer Design
"""

from __future__ import annotations

import hashlib
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from runtime.repair.entity_type_mapping import derive_entity_type
from runtime.repair.status_derivation import derive_status
from runtime.repair.types import (
    RepairContext,
    RepairLog,
    RepairLogEntry,
    RepairRequiredError,
    RepairType,
    RepairedTypedIR,
)


def _generate_repair_id() -> str:
    """Generate unique repair ID in format: repair_{16 hex chars}."""
    random_hex = secrets.token_hex(8)
    return f"repair_{random_hex}"


def _is_string_null(value: Any) -> bool:
    """Check if value is the string literal 'null'."""
    return isinstance(value, str) and value == "null"


def _is_empty_string(value: Any) -> bool:
    """Check if value is an empty string."""
    return isinstance(value, str) and value == ""


def _is_missing_or_null(value: Any) -> bool:
    """Check if value is None, missing (KeyError sentinel), or string 'null'."""
    return value is None or _is_string_null(value)


class DeterministicRepairer:
    """
    Apply deterministic repairs to typed IR.

    This repairer operates between typed IR construction and validation,
    applying ONLY classified repairs:

    1. DEFAULT_INJECTION: Inject missing default values
    2. NULL_NORMALIZATION: Convert string "null" and empty strings to null
    3. CONTEXT_INJECTION: Inject worker context values
    4. ENTITY_TYPE_DERIVATION: Derive entity_type from event_type

    Invariants:
        - Only modifies defaultable, non-semantic fields
        - Logs every modification
        - Raises RepairRequiredError for non-repairable cases
        - Never creates semantic content
        - Never modifies event_type
        - Never synthesizes entity_id, claim, title, etc.

    Example:
        >>> repairer = DeterministicRepairer()
        >>> context = RepairContext(
        ...     worker_role="Reader",
        ...     slice_id="slice_001",
        ...     task_id="task_001",
        ...     snapshot_ref="snap_abc123",
        ...     audit_id="audit_001",
        ...     trace_id="trace_00000042",
        ...     repaired_at="2026-03-27T12:00:00Z",
        ... )
        >>> result = repairer.repair(typed_output_dict, context)
        >>> if isinstance(result, RepairedTypedIR):
        ...     print(f"Repaired {result.repair_log.total_repairs} issues")
        ... else:
        ...     print(f"Repair failed: {result.failure_code}")
    """

    def __init__(self) -> None:
        """Initialize the repairer."""
        pass

    def repair(
        self,
        typed_output: dict[str, Any],
        context: RepairContext,
    ) -> RepairedTypedIR | RepairRequiredError:
        """
        Apply all applicable repairs to typed output.

        Args:
            typed_output: The typed IR to repair (as dict from TypedWorkerOutput)
            context: Worker context for context injection

        Returns:
            RepairedTypedIR on success
            RepairRequiredError if model retry is required

        The repair process:
        1. Validate repairability (check for non-repairable conditions)
        2. Apply default injection repairs
        3. Apply null normalization repairs
        4. Apply context injection repairs
        5. Apply entity type derivation repairs
        6. Return repaired output with full repair log
        """
        all_entries: list[RepairLogEntry] = []
        repairs_by_type: dict[RepairType, int] = {
            RepairType.DEFAULT_INJECTION: 0,
            RepairType.NULL_NORMALIZATION: 0,
            RepairType.CONTEXT_INJECTION: 0,
            RepairType.ENTITY_TYPE_DERIVATION: 0,
            RepairType.STATUS_DERIVATION: 0,
        }

        # Work on a copy to avoid mutating input
        repaired_output = deepcopy(typed_output)

        # Repair worker-level fields
        worker_repairs = self._repair_worker_level(repaired_output, context)
        for entry in worker_repairs:
            all_entries.append(entry)
            repairs_by_type[entry.repair_type] += 1

        # Repair each candidate event
        candidate_events = repaired_output.get("candidate_events", [])
        if not isinstance(candidate_events, list):
            candidate_events = []

        for event_index, event in enumerate(candidate_events):
            if not isinstance(event, dict):
                # Skip non-object events (will be caught by validation)
                continue

            # Validate repairability first
            repairability_error = self._validate_repairability(event, event_index)
            if repairability_error is not None:
                return repairability_error

            # Apply repairs
            event_repairs = self._repair_event(event, context, event_index)
            for entry in event_repairs:
                all_entries.append(entry)
                repairs_by_type[entry.repair_type] += 1

        # Build repair log
        repair_log = RepairLog(
            trace_id=context.trace_id,
            worker_role=context.worker_role,
            total_repairs=len(all_entries),
            repairs_by_type=repairs_by_type,
            entries=tuple(all_entries),
        )

        return RepairedTypedIR(
            typed_output=repaired_output,
            repair_log=repair_log,
            repair_success=True,
        )

    def _repair_worker_level(
        self,
        output: dict[str, Any],
        context: RepairContext,
    ) -> list[RepairLogEntry]:
        """
        Repair worker-level fields (not per-event).

        Args:
            output: The typed output dict to repair (mutated in place)
            context: Repair context

        Returns:
            List of repair log entries for worker-level repairs
        """
        entries: list[RepairLogEntry] = []

        # Repair schema_version
        schema_version = output.get("schema_version")
        if _is_missing_or_null(schema_version):
            output["schema_version"] = "1.0.0"
            entries.append(
                RepairLogEntry(
                    repair_id=_generate_repair_id(),
                    trace_id=context.trace_id,
                    event_index=-1,  # Worker-level, not event-level
                    repair_type=RepairType.DEFAULT_INJECTION,
                    field_path="schema_version",
                    original_value=schema_version,
                    repaired_value="1.0.0",
                    repair_source="fixed_default",
                    repaired_at=context.repaired_at,
                )
            )

        # Repair slice_id from context
        if _is_missing_or_null(output.get("slice_id")) and context.slice_id is not None:
            output["slice_id"] = context.slice_id
            entries.append(
                RepairLogEntry(
                    repair_id=_generate_repair_id(),
                    trace_id=context.trace_id,
                    event_index=-1,
                    repair_type=RepairType.CONTEXT_INJECTION,
                    field_path="slice_id",
                    original_value=None,
                    repaired_value=context.slice_id,
                    repair_source="context",
                    repaired_at=context.repaired_at,
                )
            )

        # Repair task_id from context
        if _is_missing_or_null(output.get("task_id")) and context.task_id is not None:
            output["task_id"] = context.task_id
            entries.append(
                RepairLogEntry(
                    repair_id=_generate_repair_id(),
                    trace_id=context.trace_id,
                    event_index=-1,
                    repair_type=RepairType.CONTEXT_INJECTION,
                    field_path="task_id",
                    original_value=None,
                    repaired_value=context.task_id,
                    repair_source="context",
                    repaired_at=context.repaired_at,
                )
            )

        # Repair snapshot_ref from context
        if _is_missing_or_null(output.get("snapshot_ref")) and context.snapshot_ref is not None:
            output["snapshot_ref"] = context.snapshot_ref
            entries.append(
                RepairLogEntry(
                    repair_id=_generate_repair_id(),
                    trace_id=context.trace_id,
                    event_index=-1,
                    repair_type=RepairType.CONTEXT_INJECTION,
                    field_path="snapshot_ref",
                    original_value=None,
                    repaired_value=context.snapshot_ref,
                    repair_source="context",
                    repaired_at=context.repaired_at,
                )
            )

        return entries

    def _repair_event(
        self,
        event: dict[str, Any],
        context: RepairContext,
        event_index: int,
    ) -> list[RepairLogEntry]:
        """
        Repair a single candidate event.

        Applies repairs in order:
        1. Inject defaults
        2. Normalize nulls
        3. Inject context
        4. Derive entity_type
        5. Derive payload.status from event_type

        Args:
            event: The event dict to repair (mutated in place)
            context: Repair context
            event_index: Index in candidate_events array

        Returns:
            List of repair log entries for this event
        """
        entries: list[RepairLogEntry] = []

        # 1. Inject defaults
        default_entries = self._inject_defaults(event, context, event_index)
        entries.extend(default_entries)

        # 2. Normalize nulls
        null_entries = self._normalize_nulls(event, context, event_index)
        entries.extend(null_entries)

        # 3. Inject context
        context_entries = self._inject_context(event, context, event_index)
        entries.extend(context_entries)

        # 4. Derive entity_type
        derivation_entries = self._derive_entity_type(event, context, event_index)
        entries.extend(derivation_entries)

        # 5. Derive payload.status from event_type
        status_entries = self._derive_status(event, context, event_index)
        entries.extend(status_entries)

        return entries

    def _inject_defaults(
        self,
        event: dict[str, Any],
        context: RepairContext,
        event_index: int,
    ) -> list[RepairLogEntry]:
        """
        Inject default values for missing fields.

        From repairability_boundary.md §3.1:
        - schema_version: "1.0.0"
        - acceptance: Full pending structure
        """
        entries: list[RepairLogEntry] = []

        # Inject default acceptance metadata
        acceptance = event.get("acceptance")
        if _is_missing_or_null(acceptance):
            default_acceptance = {
                "status": "pending",
                "decided_at": None,
                "decided_by": None,
                "reason": None,
            }
            event["acceptance"] = default_acceptance
            entries.append(
                RepairLogEntry(
                    repair_id=_generate_repair_id(),
                    trace_id=context.trace_id,
                    event_index=event_index,
                    repair_type=RepairType.DEFAULT_INJECTION,
                    field_path="acceptance",
                    original_value=acceptance,
                    repaired_value=default_acceptance,
                    repair_source="fixed_default",
                    repaired_at=context.repaired_at,
                )
            )
        elif isinstance(acceptance, dict):
            # Ensure all acceptance fields exist
            if "status" not in acceptance or acceptance.get("status") is None:
                event["acceptance"]["status"] = "pending"
                entries.append(
                    RepairLogEntry(
                        repair_id=_generate_repair_id(),
                        trace_id=context.trace_id,
                        event_index=event_index,
                        repair_type=RepairType.DEFAULT_INJECTION,
                        field_path="acceptance.status",
                        original_value=acceptance.get("status"),
                        repaired_value="pending",
                        repair_source="fixed_default",
                        repaired_at=context.repaired_at,
                    )
                )

        return entries

    def _normalize_nulls(
        self,
        event: dict[str, Any],
        context: RepairContext,
        event_index: int,
    ) -> list[RepairLogEntry]:
        """
        Normalize string "null" and empty strings to null.

        From repairability_boundary.md §3.2:
        - "null" (string literal) → null for nullable fields
        - "" (empty string) → null for decided_at, decided_by, reason
        """
        entries: list[RepairLogEntry] = []

        # Normalize top-level nullable fields
        nullable_fields = [
            "entity_id",
            "entity_type",
            "title",
            "claim",
            "summary",
            "severity",
            "source_path",
            "snapshot_ref",
        ]

        for field in nullable_fields:
            value = event.get(field)
            if _is_string_null(value):
                event[field] = None
                entries.append(
                    RepairLogEntry(
                        repair_id=_generate_repair_id(),
                        trace_id=context.trace_id,
                        event_index=event_index,
                        repair_type=RepairType.NULL_NORMALIZATION,
                        field_path=field,
                        original_value=value,
                        repaired_value=None,
                        repair_source="fixed_default",
                        repaired_at=context.repaired_at,
                    )
                )

        # Normalize acceptance sub-fields
        acceptance = event.get("acceptance")
        if isinstance(acceptance, dict):
            acceptance_string_null_fields = ["decided_at", "decided_by", "reason"]
            for field in acceptance_string_null_fields:
                value = acceptance.get(field)
                if _is_string_null(value) or _is_empty_string(value):
                    event["acceptance"][field] = None
                    entries.append(
                        RepairLogEntry(
                            repair_id=_generate_repair_id(),
                            trace_id=context.trace_id,
                            event_index=event_index,
                            repair_type=RepairType.NULL_NORMALIZATION,
                            field_path=f"acceptance.{field}",
                            original_value=value,
                            repaired_value=None,
                            repair_source="fixed_default",
                            repaired_at=context.repaired_at,
                        )
                    )

        return entries

    def _inject_context(
        self,
        event: dict[str, Any],
        context: RepairContext,
        event_index: int,
    ) -> list[RepairLogEntry]:
        """
        Inject context values for missing fields.

        From repairability_boundary.md §3.3:
        - snapshot_ref from RepairContext.snapshot_ref
        - slice_id from RepairContext.slice_id
        - task_id from RepairContext.task_id
        """
        entries: list[RepairLogEntry] = []

        # Inject snapshot_ref from context
        if _is_missing_or_null(event.get("snapshot_ref")) and context.snapshot_ref is not None:
            event["snapshot_ref"] = context.snapshot_ref
            entries.append(
                RepairLogEntry(
                    repair_id=_generate_repair_id(),
                    trace_id=context.trace_id,
                    event_index=event_index,
                    repair_type=RepairType.CONTEXT_INJECTION,
                    field_path="snapshot_ref",
                    original_value=event.get("snapshot_ref"),
                    repaired_value=context.snapshot_ref,
                    repair_source="context",
                    repaired_at=context.repaired_at,
                )
            )

        return entries

    def _derive_entity_type(
        self,
        event: dict[str, Any],
        context: RepairContext,
        event_index: int,
    ) -> list[RepairLogEntry]:
        """
        Derive entity_type from event_type if missing.

        From repairability_boundary.md §3.4:
        - Uses deterministic mapping from event_type to entity_type
        """
        entries: list[RepairLogEntry] = []

        # Only derive if entity_type is missing or null
        entity_type = event.get("entity_type")
        if _is_missing_or_null(entity_type):
            event_type = event.get("event_type")
            if event_type is not None:
                derived_type = derive_entity_type(event_type)
                if derived_type is not None:
                    event["entity_type"] = derived_type
                    entries.append(
                        RepairLogEntry(
                            repair_id=_generate_repair_id(),
                            trace_id=context.trace_id,
                            event_index=event_index,
                            repair_type=RepairType.ENTITY_TYPE_DERIVATION,
                            field_path="entity_type",
                            original_value=entity_type,
                            repaired_value=derived_type,
                            repair_source="derivation",
                            repaired_at=context.repaired_at,
                        )
                    )

        return entries

    def _derive_status(
        self,
        event: dict[str, Any],
        context: RepairContext,
        event_index: int,
    ) -> list[RepairLogEntry]:
        """
        Derive payload.status from event_type if missing.

        Status derivation is deterministic and non-semantic:
        - event_type "observation.verified" implies status "verified"
        - event_type "hypothesis.supported" implies status "supported"
        - etc.

        This repair addresses the common case where models emit the correct
        event_type but omit the redundant status field in payload.
        """
        entries: list[RepairLogEntry] = []

        # Derive status from event_type first
        event_type = event.get("event_type")
        if event_type is None:
            return entries

        derived_status = derive_status(event_type)
        if derived_status is None:
            return entries

        # Get or create payload dict
        payload = event.get("payload")
        if payload is None or not isinstance(payload, dict):
            # Create payload if missing
            payload = {}
            event["payload"] = payload
            # Log that we created the payload
            entries.append(
                RepairLogEntry(
                    repair_id=_generate_repair_id(),
                    trace_id=context.trace_id,
                    event_index=event_index,
                    repair_type=RepairType.DEFAULT_INJECTION,
                    field_path="payload",
                    original_value=None,
                    repaired_value={},
                    repair_source="fixed_default",
                    repaired_at=context.repaired_at,
                )
            )

        # Check if status is missing or null
        current_status = payload.get("status")
        if not _is_missing_or_null(current_status):
            return entries

        # Apply the status repair
        event["payload"]["status"] = derived_status
        entries.append(
            RepairLogEntry(
                repair_id=_generate_repair_id(),
                trace_id=context.trace_id,
                event_index=event_index,
                repair_type=RepairType.STATUS_DERIVATION,
                field_path="payload.status",
                original_value=current_status,
                repaired_value=derived_status,
                repair_source="derivation",
                repaired_at=context.repaired_at,
            )
        )

        return entries

    def _validate_repairability(
        self,
        event: dict[str, Any],
        event_index: int,
    ) -> RepairRequiredError | None:
        """
        Check for non-repairable conditions.

        From repairability_boundary.md §4:
        - Prohibited: Content creation (entity_id, claim, title, etc.)
        - Prohibited: Classification changes (event_type)
        - Prohibited: State transitions (acceptance.status not pending)
        - Prohibited: Reference resolution

        Returns:
            RepairRequiredError if non-repairable condition found
            None if repair can proceed
        """
        # Check for missing event_type (required, non-repairable)
        event_type = event.get("event_type")
        if event_type is None:
            return RepairRequiredError(
                failure_code="missing_event_type",
                field_path="event_type",
                message="event_type is required and cannot be synthesized",
                event_index=event_index,
                retryable=True,
            )

        # Check acceptance status (must be pending for new events)
        acceptance = event.get("acceptance")
        if isinstance(acceptance, dict):
            status = acceptance.get("status")
            if status is not None and status != "pending":
                return RepairRequiredError(
                    failure_code="candidate_event_not_pending",
                    field_path="acceptance.status",
                    message=(
                        f"process_candidate_events accepts only candidate events "
                        f"with acceptance.status='pending', got '{status}'"
                    ),
                    event_index=event_index,
                    retryable=False,  # Fatal - invalid initial state
                )

            # Check for invalid acceptance actor (decided_by should be null for pending)
            decided_by = acceptance.get("decided_by")
            if decided_by is not None and status == "pending":
                return RepairRequiredError(
                    failure_code="invalid_acceptance_actor",
                    field_path="acceptance.decided_by",
                    message="acceptance.decided_by must be null for pending events",
                    event_index=event_index,
                    retryable=False,  # Fatal - invalid initial state
                )

        # No non-repairable conditions found
        return None


def create_repair_context(
    worker_role: str,
    trace_id: str,
    *,
    slice_id: str | None = None,
    task_id: str | None = None,
    snapshot_ref: str | None = None,
    audit_id: str | None = None,
    repaired_at: str | None = None,
) -> RepairContext:
    """
    Factory function to create a RepairContext with defaults.

    Args:
        worker_role: Role of the worker
        trace_id: Correlation ID for tracing
        slice_id: Optional slice identifier
        task_id: Optional task identifier
        snapshot_ref: Optional snapshot reference
        audit_id: Optional audit identifier
        repaired_at: Optional ISO timestamp (defaults to now)

    Returns:
        RepairContext instance

    Example:
        >>> context = create_repair_context(
        ...     worker_role="Reader",
        ...     trace_id="trace_001",
        ...     snapshot_ref="snap_abc",
        ... )
    """
    if repaired_at is None:
        repaired_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return RepairContext(
        worker_role=worker_role,
        slice_id=slice_id,
        task_id=task_id,
        snapshot_ref=snapshot_ref,
        audit_id=audit_id,
        trace_id=trace_id,
        repaired_at=repaired_at,
    )
