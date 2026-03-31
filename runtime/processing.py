from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from runtime.canonicalization import canonicalize_event
    from runtime.event_store import (
        AppendResult,
        EventStore,
        EventStoreError,
        atomic_write_text,
        workspace_lock,
    )
    from runtime.projector import ProjectionResult, StateProjector
    from runtime.rejection import (
        RejectionChain,
        RejectionDetail,
        RejectionReason,
        RejectionStage,
        classify_policy_rejection,
        classify_schema_rejection,
        classify_transport_rejection,
    )
    from runtime.run_ledger import RunLedger, RunLedgerError, WorkerExecutionTraceContext
    from runtime.validators import ValidationIssue, ValidatorSuite
except ModuleNotFoundError:  # pragma: no cover - allows direct script execution.
    from canonicalization import canonicalize_event
    from event_store import AppendResult, EventStore, EventStoreError, atomic_write_text, workspace_lock
    from projector import ProjectionResult, StateProjector
    from rejection import (
        RejectionChain,
        RejectionDetail,
        RejectionReason,
        RejectionStage,
        classify_policy_rejection,
        classify_schema_rejection,
        classify_transport_rejection,
    )
    from run_ledger import RunLedger, RunLedgerError, WorkerExecutionTraceContext
    from validators import ValidationIssue, ValidatorSuite


SYSTEM_ACCEPTOR = {
    "actor_type": "system",
    "actor_id": "runtime.process_candidate_events",
    "role": None,
}
ACCEPTANCE_REASON = "accepted_by_validator_suite"

_VALIDATOR_CODE_TO_REJECTION: dict[str, tuple[RejectionReason, RejectionStage]] = {
    # Processing layer
    "candidate_event_not_object":       (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "missing_acceptance_metadata":      (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "candidate_event_not_pending":      (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    # Schema validator
    "schema_validation_failed":         (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "payload_entity_id_mismatch":       (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "payload_audit_id_mismatch":        (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    # Source binding validator
    "missing_source_refs":              (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "missing_line_range":               (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "incomplete_line_range":             (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "invalid_line_range_type":           (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "invalid_line_range":               (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "inferred_line_range":              (RejectionReason.TRANSPORT_REJECTED, RejectionStage.TRANSPORT),
    "invalid_column_range":             (RejectionReason.TRANSPORT_REJECTED, RejectionStage.TRANSPORT),
    # Duplicate validator
    "duplicate_submission":             (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "event_id_conflict":                (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "idempotency_conflict":             (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    # Transition validator
    "invalid_transition":               (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "missing_worker_role":              (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "worker_event_forbidden":           (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "referenced_entity_missing":        (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "referenced_entity_wrong_state":    (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "referenced_entity_field_forbidden": (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "field_dependency_failed":          (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "forbidden_entity_promotion":       (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "promotion_missing_observation_id":  (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "promotion_candidate_not_found":    (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "promotion_observation_not_found":  (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    "promotion_observation_link_mismatch": (RejectionReason.POLICY_REJECTED,   RejectionStage.POLICY),
    # Event store
    "append_failed":                    (RejectionReason.TRANSPORT_REJECTED, RejectionStage.TRANSPORT),
    "append_not_persisted":             (RejectionReason.TRANSPORT_REJECTED, RejectionStage.TRANSPORT),
    # Semantic content
    "OBS_empty_statement":              (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "OBS_no_source_refs":               (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "OBS_no_source":                    (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "OBS_fake_binding":                 (RejectionReason.TRANSPORT_REJECTED, RejectionStage.TRANSPORT),
    "OBS_empty_semantic":               (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "HYP_empty_statement":              (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    "HYP_empty_rationale":              (RejectionReason.SCHEMA_INVALID,    RejectionStage.SCHEMA),
    # State projection
    "state_projection_failed":          (RejectionReason.TRANSPORT_REJECTED, RejectionStage.TRANSPORT),
}
_FALLBACK = (RejectionReason.SCHEMA_INVALID, RejectionStage.SCHEMA)


class CandidateEventProcessingError(Exception):
    """Raised when a candidate-event processing request is structurally invalid."""


@dataclass(frozen=True)
class CandidateEventOutcome:
    index: int
    event_id: str | None
    audit_id: str | None
    entity_type: str | None
    entity_id: str | None
    event_type: str | None
    outcome: str
    issues: tuple[ValidationIssue, ...]
    append_result: AppendResult | None


@dataclass(frozen=True)
class CandidateEventProcessingResult:
    audit_id: str | None
    total_candidate_events: int
    accepted_events: int
    rejected_events: int
    projection_result: ProjectionResult | None
    event_outcomes: tuple[CandidateEventOutcome, ...]
    trace_entry_id: str | None


def process_candidate_events(
    root_dir: str | Path,
    candidate_events: Iterable[dict[str, Any]],
    *,
    audit_id: str | None = None,
    events_dir: str | Path = "events",
    state_dir: str | Path = "state",
    runs_dir: str | Path = "runs",
    trace_context: WorkerExecutionTraceContext | dict[str, Any] | None = None,
    lock_workspace: bool = True,
) -> CandidateEventProcessingResult:
    """Validate pending candidate events, append accepted events, and rebuild canonical state."""
    root_path = Path(root_dir).resolve()
    candidate_event_list = tuple(candidate_events)
    _validate_batch_audit_alignment(candidate_event_list, audit_id=audit_id)
    normalized_trace_context = _coerce_trace_context(trace_context)

    lock_context = (
        workspace_lock(root_path, owner="processing.process_candidate_events")
        if lock_workspace
        else nullcontext()
    )
    with lock_context:
        validator_suite = ValidatorSuite(root_path, events_dir=events_dir)
        event_store = EventStore(root_path, events_dir=events_dir)
        event_store.recover_ledger()

        event_outcomes: list[CandidateEventOutcome] = []
        appended_audit_ids: set[str] = set()

        for index, candidate_event in enumerate(candidate_event_list):
            accepted_event, preparation_issues = _prepare_accepted_event(candidate_event)
            if preparation_issues:
                event_outcomes.append(
                    _build_outcome(
                        index=index,
                        event=candidate_event,
                        outcome="rejected",
                        issues=preparation_issues,
                    )
                )
                continue

            validation_result = validator_suite.validate_event(accepted_event)
            if not validation_result.is_valid:
                event_outcomes.append(
                    _build_outcome(
                        index=index,
                        event=accepted_event,
                        outcome="rejected",
                        issues=validation_result.issues,
                    )
                )
                continue

            try:
                append_result = event_store.append_event(accepted_event)
            except EventStoreError as exc:
                event_outcomes.append(
                    _build_outcome(
                        index=index,
                        event=accepted_event,
                        outcome="rejected",
                        issues=(
                            ValidationIssue(
                                validator="event_store",
                                code="append_failed",
                                message=str(exc),
                            ),
                        ),
                    )
                )
                continue

            if append_result.outcome != "appended":
                event_outcomes.append(
                    _build_outcome(
                        index=index,
                        event=accepted_event,
                        outcome="rejected",
                        issues=(
                            ValidationIssue(
                                validator="event_store",
                                code="append_not_persisted",
                                message=(
                                    f"Event '{accepted_event['id']}' was not appended because the ledger "
                                    f"reported outcome '{append_result.outcome}'."
                                ),
                            ),
                        ),
                        append_result=append_result,
                    )
                )
                continue

            appended_audit_ids.add(accepted_event["audit_id"])
            event_outcomes.append(
                _build_outcome(
                    index=index,
                    event=accepted_event,
                    outcome="accepted",
                    issues=(),
                    append_result=append_result,
                )
            )

        projection_result: ProjectionResult | None = None
        projection_audit_id = _resolve_projection_audit_id(
            requested_audit_id=audit_id,
            appended_audit_ids=appended_audit_ids,
        )
        if projection_audit_id is not None and appended_audit_ids:
            try:
                projection_result = recover_runtime_state(
                    root_path,
                    audit_id=projection_audit_id,
                    events_dir=events_dir,
                    state_dir=state_dir,
                    lock_workspace=False,
                )
            except CandidateEventProcessingError as exc:
                raise CandidateEventProcessingError(
                    "Accepted events were persisted, but canonical state projection failed. "
                    "Run rebuild-state to recover canonical_state.json from the accepted event log. "
                    f"cause: {exc}"
                ) from exc

        accepted_events = sum(1 for outcome in event_outcomes if outcome.outcome == "accepted")
        trace_entry_id: str | None = None
        if normalized_trace_context is not None:
            trace_entry_id = _record_trace_entry(
                root_path=root_path,
                runs_dir=runs_dir,
                trace_context=normalized_trace_context,
                event_outcomes=event_outcomes,
                total_candidate_events=len(candidate_event_list),
                accepted_events=accepted_events,
                rejected_events=len(candidate_event_list) - accepted_events,
            )
        return CandidateEventProcessingResult(
            audit_id=projection_audit_id,
            total_candidate_events=len(candidate_event_list),
            accepted_events=accepted_events,
            rejected_events=len(candidate_event_list) - accepted_events,
            projection_result=projection_result,
            event_outcomes=tuple(event_outcomes),
            trace_entry_id=trace_entry_id,
        )


def recover_runtime_state(
    root_dir: str | Path,
    *,
    audit_id: str | None = None,
    events_dir: str | Path = "events",
    state_dir: str | Path = "state",
    lock_workspace: bool = True,
) -> ProjectionResult:
    """Rebuild canonical_state.json atomically from the accepted event log."""
    root_path = Path(root_dir).resolve()
    lock_context = (
        workspace_lock(root_path, owner="processing.recover_runtime_state")
        if lock_workspace
        else nullcontext()
    )
    with lock_context:
        event_store = EventStore(root_path, events_dir=events_dir)
        event_store.recover_ledger()
        projector = StateProjector(root_path, events_dir=events_dir, state_dir=state_dir)
        try:
            state, total_events, accepted_events = projector.build_state(audit_id=audit_id)
        except Exception as exc:  # pragma: no cover - surfaced to callers with deterministic message.
            raise CandidateEventProcessingError(
                f"Failed to rebuild canonical state from the accepted event log: {exc}"
            ) from exc
        return _write_projection_atomically(
            projector=projector,
            state=state,
            total_events=total_events,
            accepted_events=accepted_events,
            audit_id=audit_id,
        )


def _validate_batch_audit_alignment(
    candidate_events: tuple[dict[str, Any], ...],
    audit_id: str | None,
) -> None:
    batch_audit_ids = sorted(
        {
            candidate_event.get("audit_id")
            for candidate_event in candidate_events
            if isinstance(candidate_event, dict) and isinstance(candidate_event.get("audit_id"), str)
        }
    )

    if audit_id is not None:
        mismatched = [candidate_audit_id for candidate_audit_id in batch_audit_ids if candidate_audit_id != audit_id]
        if mismatched:
            raise CandidateEventProcessingError(
                "All candidate events must match the requested audit_id "
                f"'{audit_id}', found: {', '.join(mismatched)}"
            )
        return

    if len(batch_audit_ids) > 1:
        raise CandidateEventProcessingError(
            "process_candidate_events requires a single audit id per call when audit_id is not provided, "
            f"found: {', '.join(batch_audit_ids)}"
        )


def _prepare_accepted_event(
    candidate_event: dict[str, Any],
) -> tuple[dict[str, Any] | None, tuple[ValidationIssue, ...]]:
    if not isinstance(candidate_event, dict):
        return None, (
            ValidationIssue(
                validator="processing",
                code="candidate_event_not_object",
                message="Candidate events must be JSON objects.",
            ),
        )

    normalized_event = json.loads(json.dumps(candidate_event))
    acceptance = normalized_event.get("acceptance")
    if not isinstance(acceptance, dict):
        return None, (
            ValidationIssue(
                validator="processing",
                code="missing_acceptance_metadata",
                message="Candidate events must include acceptance metadata.",
                path="acceptance",
            ),
        )

    if acceptance.get("status") != "pending":
        return None, (
            ValidationIssue(
                validator="processing",
                code="candidate_event_not_pending",
                message="process_candidate_events accepts only candidate events with acceptance.status='pending'.",
                path="acceptance.status",
            ),
        )

    normalized_event["acceptance"] = {
        "status": "accepted",
        "decided_at": normalized_event.get("occurred_at"),
        "decided_by": dict(SYSTEM_ACCEPTOR),
        "reason": ACCEPTANCE_REASON,
    }
    return canonicalize_event(normalized_event), ()


def _resolve_projection_audit_id(
    requested_audit_id: str | None,
    appended_audit_ids: set[str],
) -> str | None:
    if requested_audit_id is not None:
        return requested_audit_id
    if not appended_audit_ids:
        return None
    if len(appended_audit_ids) > 1:
        raise CandidateEventProcessingError(
            "Accepted events span multiple audits; an explicit audit_id is required for projection."
        )
    return next(iter(appended_audit_ids))


def _coerce_trace_context(
    trace_context: WorkerExecutionTraceContext | dict[str, Any] | None,
) -> WorkerExecutionTraceContext | None:
    if trace_context is None:
        return None
    if isinstance(trace_context, WorkerExecutionTraceContext):
        return trace_context
    if isinstance(trace_context, dict):
        return WorkerExecutionTraceContext.from_dict(trace_context)
    raise CandidateEventProcessingError("trace_context must be a WorkerExecutionTraceContext, dict, or null.")


def _record_trace_entry(
    *,
    root_path: Path,
    runs_dir: str | Path,
    trace_context: WorkerExecutionTraceContext,
    event_outcomes: list[CandidateEventOutcome],
    total_candidate_events: int,
    accepted_events: int,
    rejected_events: int,
) -> str:
    run_ledger = RunLedger(root_path, runs_dir=runs_dir)
    try:
        trace_entry = run_ledger.record_worker_execution(
            trace_context=trace_context,
            total_candidate_events=total_candidate_events,
            accepted_events=accepted_events,
            rejected_events=rejected_events,
            event_outcomes=[_trace_outcome(outcome) for outcome in event_outcomes],
        )
    except RunLedgerError as exc:
        raise CandidateEventProcessingError(f"Failed to record forensic trace entry: {exc}") from exc
    return trace_entry["entry_id"]


def _trace_rejection(issues: tuple[ValidationIssue, ...]) -> dict[str, Any] | None:
    if not issues:
        return None
    primary = issues[0]
    reason, stage = _VALIDATOR_CODE_TO_REJECTION.get(primary.code, _FALLBACK)
    return {
        "rejection_code": reason.value,
        "rejection_layer": stage.value,
        "rejection_message": primary.message[:120] if len(primary.message) > 120 else primary.message,
        "validator": primary.validator,
        "validator_code": primary.code,
        "all_issue_codes": [issue.code for issue in issues],
    }


def _trace_outcome(outcome: CandidateEventOutcome) -> dict[str, Any]:
    rejection_detail = _trace_rejection(outcome.issues) if outcome.outcome == "rejected" else None
    return {
        "event_id": outcome.event_id,
        "event_type": outcome.event_type,
        "entity_type": outcome.entity_type,
        "entity_id": outcome.entity_id,
        "outcome": outcome.outcome,
        "rejection": rejection_detail,
        "issue_codes": [issue.code for issue in outcome.issues],
        "ledger_line_number": outcome.append_result.line_number if outcome.append_result is not None else None,
        "ledger_path": str(outcome.append_result.ledger_path) if outcome.append_result is not None else None,
    }


def _build_outcome(
    *,
    index: int,
    event: dict[str, Any] | None,
    outcome: str,
    issues: tuple[ValidationIssue, ...],
    append_result: AppendResult | None = None,
) -> CandidateEventOutcome:
    return CandidateEventOutcome(
        index=index,
        event_id=_read_string_field(event, "id"),
        audit_id=_read_string_field(event, "audit_id"),
        entity_type=_read_string_field(event, "entity_type"),
        entity_id=_read_string_field(event, "entity_id"),
        event_type=_read_string_field(event, "event_type"),
        outcome=outcome,
        issues=issues,
        append_result=append_result,
    )


def _read_string_field(event: dict[str, Any] | None, field_name: str) -> str | None:
    if not isinstance(event, dict):
        return None
    value = event.get(field_name)
    return value if isinstance(value, str) else None


def _write_projection_atomically(
    *,
    projector: StateProjector,
    state: dict[str, Any],
    total_events: int,
    accepted_events: int,
    audit_id: str | None,
) -> ProjectionResult:
    compact_json = projector._serialize_compact(state)
    pretty_json = projector._serialize_pretty(state)
    projection_id = hashlib.sha256(compact_json.encode("ascii")).hexdigest()[:16]

    atomic_write_text(projector.canonical_state_path, pretty_json)
    snapshot_path = projector.projections_dir / f"canonical_state.{projection_id}.json"
    atomic_write_text(snapshot_path, pretty_json)

    resolved_audit_id = audit_id
    audit = state.get("audit")
    if resolved_audit_id is None and isinstance(audit, dict):
        audit_value = audit.get("id")
        if isinstance(audit_value, str) and audit_value:
            resolved_audit_id = audit_value

    return ProjectionResult(
        audit_id=resolved_audit_id,
        total_events=total_events,
        accepted_events=accepted_events,
        applied_events=accepted_events,
        projection_id=projection_id,
        canonical_state_path=projector.canonical_state_path,
        snapshot_path=snapshot_path,
    )
