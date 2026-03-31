"""
Type definitions for the Deterministic Repair Layer (STEP 5).

All types are defined here to ensure single source of truth for the repair layer contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RepairType(Enum):
    """Classification of repair operation types."""

    DEFAULT_INJECTION = "default_injection"
    NULL_NORMALIZATION = "null_normalization"
    CONTEXT_INJECTION = "context_injection"
    ENTITY_TYPE_DERIVATION = "entity_type_derivation"
    STATUS_DERIVATION = "status_derivation"


class RepairRequiredError(Exception):
    """
    Raised when repair is not possible; model retry required.

    This error indicates that the typed IR contains a defect that
    cannot be deterministically repaired and requires model re-invocation.

    Attributes:
        failure_code: Code identifying the failure type (e.g., 'missing_entity_id')
        field_path: JSON path to the problematic field
        message: Human-readable error message
        event_index: Index in candidate_events array, if applicable
        retryable: Whether model retry may resolve the issue
    """

    def __init__(
        self,
        failure_code: str,
        field_path: str,
        message: str,
        event_index: int | None = None,
        retryable: bool = True,
    ) -> None:
        self.failure_code = failure_code
        self.field_path = field_path
        self.message = message
        self.event_index = event_index
        self.retryable = retryable
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Serialize error to dictionary for logging/telemetry."""
        return {
            "failure_code": self.failure_code,
            "field_path": self.field_path,
            "message": self.message,
            "event_index": self.event_index,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class RepairLogEntry:
    """
    Single repair operation log entry.

    Every repair applied by DeterministicRepairer creates one log entry
    for traceability and audit purposes.

    Attributes:
        repair_id: Unique identifier for this repair (format: repair_{16 hex chars})
        trace_id: Correlation ID with invocation trace
        event_index: Index of the repaired event in candidate_events array
        repair_type: Classification of repair operation
        field_path: JSON path to the repaired field
        original_value: Value before repair (for audit)
        repaired_value: Value after repair (for audit)
        repair_source: Where the repair value came from
            - "fixed_default": Hard-coded default value
            - "context": Value from RepairContext
            - "derivation": Computed from other fields
        repaired_at: ISO 8601 timestamp when repair was applied
    """

    repair_id: str
    trace_id: str
    event_index: int
    repair_type: RepairType
    field_path: str
    original_value: Any | None
    repaired_value: Any | None
    repair_source: str
    repaired_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize log entry to dictionary."""
        return {
            "repair_id": self.repair_id,
            "trace_id": self.trace_id,
            "event_index": self.event_index,
            "repair_type": self.repair_type.value,
            "field_path": self.field_path,
            "original_value": self.original_value,
            "repaired_value": self.repaired_value,
            "repair_source": self.repair_source,
            "repaired_at": self.repaired_at,
        }


@dataclass(frozen=True)
class RepairLog:
    """
    Complete repair log for a worker output.

    Aggregates all repair operations performed on a TypedWorkerOutput
    with statistics by repair type.

    Attributes:
        trace_id: Correlation ID with invocation trace
        worker_role: Role of the worker that produced the output
        total_repairs: Total number of repair operations
        repairs_by_type: Count of repairs grouped by RepairType
        entries: Individual repair log entries in order applied
    """

    trace_id: str
    worker_role: str
    total_repairs: int
    repairs_by_type: dict[RepairType, int]
    entries: tuple[RepairLogEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize log to dictionary."""
        return {
            "trace_id": self.trace_id,
            "worker_role": self.worker_role,
            "total_repairs": self.total_repairs,
            "repairs_by_type": {
                rt.value: count for rt, count in self.repairs_by_type.items()
            },
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass(frozen=True)
class RepairContext:
    """
    Context available to repair operations.

    Provides worker-level context values that can be injected into
    events that are missing optional fields.

    Attributes:
        worker_role: Role of the worker (e.g., "Reader", "Verifier")
        slice_id: Slice identifier from worker input
        task_id: Task identifier from worker input
        snapshot_ref: Source snapshot reference from worker context
        audit_id: Audit identifier from worker context
        trace_id: Correlation ID for tracing
        repaired_at: ISO 8601 timestamp for repair log entries
    """

    worker_role: str
    slice_id: str | None
    task_id: str | None
    snapshot_ref: str | None
    audit_id: str | None
    trace_id: str
    repaired_at: str


@dataclass(frozen=True)
class RepairedTypedIR:
    """
    Result of successful repair operation.

    Contains the repaired typed output along with complete repair log
    for traceability.

    Attributes:
        typed_output: The repaired TypedWorkerOutput (with repairs applied)
        repair_log: Complete log of all repairs applied
        repair_success: Always True for successful repairs
    """

    typed_output: dict[str, Any]  # Repaired TypedWorkerOutput as dict
    repair_log: RepairLog
    repair_success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize result to dictionary."""
        return {
            "typed_output": self.typed_output,
            "repair_log": self.repair_log.to_dict(),
            "repair_success": self.repair_success,
        }
