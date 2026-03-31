from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from runtime.secret_redaction import redact_trace_entry


RUN_LEDGER_SCHEMA_VERSION = "1.0.0"


class RunLedgerError(Exception):
    """Raised when the forensic run ledger cannot be read or updated."""


@dataclass(frozen=True)
class RunStartResult:
    run_id: str
    entry_id: str
    sequence: int
    ledger_path: Path


@dataclass(frozen=True)
class WorkerExecutionTraceContext:
    run_id: str
    audit_id: str
    task_id: str
    slice_id: str
    worker_role: str
    adapter_invocation: dict[str, Any]
    input_digest: str | None
    output_digest: str | None
    slice_fingerprint: str | None = None
    snapshot_ref: str | None = None
    prompt_digest: str | None = None
    raw_output_digest: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("run_id", "audit_id", "task_id", "slice_id", "worker_role"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RunLedgerError(f"trace_context.{field_name} must be a non-empty string.")
        if not isinstance(self.adapter_invocation, dict):
            raise RunLedgerError("trace_context.adapter_invocation must be a JSON object.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "audit_id": self.audit_id,
            "task_id": self.task_id,
            "slice_id": self.slice_id,
            "worker_role": self.worker_role,
            "adapter_invocation": _normalize(self.adapter_invocation),
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "slice_fingerprint": self.slice_fingerprint,
            "snapshot_ref": self.snapshot_ref,
            "prompt_digest": self.prompt_digest,
            "raw_output_digest": self.raw_output_digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkerExecutionTraceContext":
        if not isinstance(payload, dict):
            raise RunLedgerError("trace_context must be a JSON object.")
        return cls(
            run_id=payload["run_id"],
            audit_id=payload["audit_id"],
            task_id=payload["task_id"],
            slice_id=payload["slice_id"],
            worker_role=payload["worker_role"],
            adapter_invocation=_normalize(payload["adapter_invocation"]),
            input_digest=_optional_string(payload.get("input_digest")),
            output_digest=_optional_string(payload.get("output_digest")),
            slice_fingerprint=_optional_string(payload.get("slice_fingerprint")),
            snapshot_ref=_optional_string(payload.get("snapshot_ref")),
            prompt_digest=_optional_string(payload.get("prompt_digest")),
            raw_output_digest=_optional_string(payload.get("raw_output_digest")),
        )


def _normalize(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RunLedgerError("optional trace fields must be strings or null.")
    stripped = value.strip()
    return stripped or None


class RunLedger:
    """Append-only forensic ledger for audit runs and worker executions."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        runs_dir: str | Path = "runs",
        ledger_name: str = "run_ledger.ndjson",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.runs_dir = (self.root_dir / runs_dir).resolve()
        self.ledger_path = (self.runs_dir / ledger_name).resolve()

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path.touch(exist_ok=True)

    def read_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        with self.ledger_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise RunLedgerError(
                        f"Invalid NDJSON in {self.ledger_path} at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(entry, dict):
                    raise RunLedgerError(
                        f"Run ledger entries must be JSON objects in {self.ledger_path} at line {line_number}."
                    )
                entries.append(entry)
        return entries

    def start_run(
        self,
        *,
        audit_id: str,
        snapshot_ref: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunStartResult:
        if not isinstance(audit_id, str) or not audit_id.strip():
            raise RunLedgerError("audit_id must be a non-empty string.")
        if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
            raise RunLedgerError("snapshot_ref must be a non-empty string.")

        entries = self.read_entries()
        normalized_metadata = _normalize(metadata or {})
        run_id_value = run_id or self._next_run_id(entries)

        existing = next(
            (
                entry
                for entry in entries
                if entry.get("entry_type") == "run_started" and entry.get("run_id") == run_id_value
            ),
            None,
        )
        if existing is not None:
            if existing.get("audit_id") != audit_id or existing.get("snapshot_ref") != snapshot_ref:
                raise RunLedgerError(
                    f"run_id '{run_id_value}' already exists with different run metadata."
                )
            return RunStartResult(
                run_id=run_id_value,
                entry_id=existing["entry_id"],
                sequence=existing["sequence"],
                ledger_path=self.ledger_path,
            )

        sequence = self._next_sequence(entries)
        entry = {
            "schema_version": RUN_LEDGER_SCHEMA_VERSION,
            "entry_type": "run_started",
            "entry_id": self._entry_id(sequence),
            "sequence": sequence,
            "run_id": run_id_value,
            "audit_id": audit_id,
            "snapshot_ref": snapshot_ref,
            "metadata": normalized_metadata,
        }
        self._append_entry(entry)
        return RunStartResult(
            run_id=run_id_value,
            entry_id=entry["entry_id"],
            sequence=sequence,
            ledger_path=self.ledger_path,
        )

    def record_worker_execution(
        self,
        *,
        trace_context: WorkerExecutionTraceContext,
        total_candidate_events: int,
        accepted_events: int,
        rejected_events: int,
        event_outcomes: Iterable[dict[str, Any]],
        execution_status: str = "succeeded",
        failure_stage: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(total_candidate_events, int) or total_candidate_events < 0:
            raise RunLedgerError("total_candidate_events must be a non-negative integer.")
        if not isinstance(accepted_events, int) or accepted_events < 0:
            raise RunLedgerError("accepted_events must be a non-negative integer.")
        if not isinstance(rejected_events, int) or rejected_events < 0:
            raise RunLedgerError("rejected_events must be a non-negative integer.")
        if execution_status not in {"succeeded", "failed"}:
            raise RunLedgerError("execution_status must be 'succeeded' or 'failed'.")
        if execution_status == "succeeded":
            if failure_stage is not None:
                raise RunLedgerError("failure_stage must be null for successful worker execution entries.")
            if trace_context.input_digest is None or trace_context.output_digest is None:
                raise RunLedgerError(
                    "successful worker execution entries require input_digest and output_digest."
                )
        else:
            if not isinstance(failure_stage, str) or not failure_stage.strip():
                raise RunLedgerError("failed worker execution entries require a non-empty failure_stage.")

        entries = self.read_entries()
        sequence = self._next_sequence(entries)
        normalized_outcomes = [
            self._normalize_event_outcome(outcome)
            for outcome in event_outcomes
        ]
        entry = {
            "schema_version": RUN_LEDGER_SCHEMA_VERSION,
            "entry_type": "worker_execution",
            "entry_id": self._entry_id(sequence),
            "sequence": sequence,
            **trace_context.to_dict(),
            "execution_status": execution_status,
            "failure_stage": failure_stage.strip() if isinstance(failure_stage, str) and failure_stage.strip() else None,
            "candidate_event_count": total_candidate_events,
            "accepted_event_count": accepted_events,
            "rejected_event_count": rejected_events,
            "accepted_events": [
                outcome for outcome in normalized_outcomes if outcome["outcome"] == "accepted"
            ],
            "rejected_events": [
                outcome for outcome in normalized_outcomes if outcome["outcome"] == "rejected"
            ],
            "error_message": error_message.strip() if isinstance(error_message, str) and error_message.strip() else None,
        }
        self._append_entry(entry)
        return entry

    def record_worker_execution_failure(
        self,
        *,
        trace_context: WorkerExecutionTraceContext,
        failure_stage: str,
        error_message: str,
    ) -> dict[str, Any]:
        return self.record_worker_execution(
            trace_context=trace_context,
            total_candidate_events=0,
            accepted_events=0,
            rejected_events=0,
            event_outcomes=(),
            execution_status="failed",
            failure_stage=failure_stage,
            error_message=error_message,
        )

    def resolve_event_trace(self, event_id: str) -> dict[str, Any] | None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise RunLedgerError("event_id must be a non-empty string.")

        for entry in reversed(self.read_entries()):
            if entry.get("entry_type") != "worker_execution":
                continue
            for outcome_name in ("accepted_events", "rejected_events"):
                for event_ref in entry.get(outcome_name, []):
                    if event_ref.get("event_id") == event_id:
                        return self._build_trace_record(
                            entry,
                            event_ref,
                            processing_outcome="accepted" if outcome_name == "accepted_events" else "rejected",
                        )
        return None

    def resolve_entity_trace(
        self,
        entity_type: str,
        entity_id: str,
        *,
        preferred_event_types: Iterable[str] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(entity_type, str) or not entity_type.strip():
            raise RunLedgerError("entity_type must be a non-empty string.")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise RunLedgerError("entity_id must be a non-empty string.")

        preferred = tuple(preferred_event_types or ())
        matches: list[tuple[int, dict[str, Any]]] = []
        for entry in self.read_entries():
            if entry.get("entry_type") != "worker_execution":
                continue
            for event_ref in entry.get("accepted_events", []):
                if event_ref.get("entity_type") != entity_type or event_ref.get("entity_id") != entity_id:
                    continue
                rank = preferred.index(event_ref["event_type"]) if event_ref["event_type"] in preferred else len(preferred)
                matches.append(
                    (
                        rank,
                        self._build_trace_record(
                            entry,
                            event_ref,
                            processing_outcome="accepted",
                        ),
                    )
                )

        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1]["sequence"]), reverse=False)
        best_rank = matches[0][0]
        ranked = [item[1] for item in matches if item[0] == best_rank]
        return ranked[-1]

    def resolve_finding_trace(
        self,
        *,
        audit_id: str,
        issue_id: str,
        supporting_observation_ids: Iterable[str],
    ) -> dict[str, Any]:
        issue_trace = self.resolve_entity_trace(
            "issue",
            issue_id,
            preferred_event_types=("issue.proposed",),
        )
        observation_traces = [
            {
                "observation_id": observation_id,
                "trace": self.resolve_entity_trace(
                    "observation",
                    observation_id,
                    preferred_event_types=("observation.verified", "observation.proposed"),
                ),
            }
            for observation_id in sorted({item for item in supporting_observation_ids if isinstance(item, str) and item})
        ]
        return {
            "audit_id": audit_id,
            "issue_id": issue_id,
            "issue_trace": issue_trace,
            "supporting_observations": observation_traces,
            "resolved": issue_trace is not None and all(item["trace"] is not None for item in observation_traces),
        }

    def resolve_report_trace(self, report: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(report, dict):
            raise RunLedgerError("report must be a JSON object.")
        audit_id = report.get("source_audit_id")
        findings = report.get("findings")
        if not isinstance(audit_id, str) or not audit_id:
            raise RunLedgerError("report.source_audit_id must be a non-empty string.")
        if not isinstance(findings, list):
            raise RunLedgerError("report.findings must be a JSON array.")

        finding_traces = [
            {
                "issue_id": finding["issue_id"],
                "trace": self.resolve_finding_trace(
                    audit_id=audit_id,
                    issue_id=finding["issue_id"],
                    supporting_observation_ids=finding.get("supporting_observation_ids", []),
                ),
            }
            for finding in findings
            if isinstance(finding, dict) and isinstance(finding.get("issue_id"), str)
        ]
        return {
            "schema_version": RUN_LEDGER_SCHEMA_VERSION,
            "source_audit_id": audit_id,
            "finding_traces": finding_traces,
        }

    # =========================================================================
    # Candidate Traceability Methods (v1.2 Step 11)
    # =========================================================================

    def resolve_candidate_trace(
        self,
        *,
        audit_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        """Resolve the full lifecycle trace for a candidate entity.

        Reconstructs the candidate's journey from proposal through routing,
        verification, and final outcome (promotion or rejection).

        Args:
            audit_id: The audit ID the candidate belongs to
            candidate_id: The candidate entity ID (e.g., candidate_abc123)

        Returns:
            A trace record containing:
            - candidate_id: The candidate identifier
            - audit_id: The parent audit ID
            - proposed_trace: Trace for candidate.proposed event
            - routed_trace: Trace for candidate.routed_to_verify event (if applicable)
            - rejection_trace: Trace for candidate.rejected event (if rejected)
            - promotion_trace: Trace for candidate.promoted_to_observation event (if promoted)
            - promoted_observation_trace: Trace for the resulting observation (if promoted)
            - outcome: One of "proposed", "routed_to_verify", "rejected", "resolved_promoted"
            - resolved: True if the candidate has a terminal outcome
        """
        if not isinstance(audit_id, str) or not audit_id.strip():
            raise RunLedgerError("audit_id must be a non-empty string.")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise RunLedgerError("candidate_id must be a non-empty string.")

        result: dict[str, Any] = {
            "candidate_id": candidate_id,
            "audit_id": audit_id,
            "proposed_trace": None,
            "routed_trace": None,
            "rejection_trace": None,
            "promotion_trace": None,
            "promoted_observation_trace": None,
            "outcome": "unknown",
            "resolved": False,
        }

        # Find candidate.proposed trace
        proposed_trace = self.resolve_entity_trace(
            "candidate",
            candidate_id,
            preferred_event_types=("candidate.proposed",),
        )
        # Verify the trace actually has the expected event type
        if self._trace_matches_event_type(proposed_trace, "candidate.proposed"):
            result["proposed_trace"] = proposed_trace

        # Find candidate.routed_to_verify trace
        routed_trace = self.resolve_entity_trace(
            "candidate",
            candidate_id,
            preferred_event_types=("candidate.routed_to_verify",),
        )
        if self._trace_matches_event_type(routed_trace, "candidate.routed_to_verify"):
            result["routed_trace"] = routed_trace

        # Find candidate.rejected trace
        rejection_trace = self.resolve_entity_trace(
            "candidate",
            candidate_id,
            preferred_event_types=("candidate.rejected",),
        )
        if self._trace_matches_event_type(rejection_trace, "candidate.rejected"):
            result["rejection_trace"] = rejection_trace

        # Find candidate.promoted_to_observation trace
        promotion_trace = self.resolve_entity_trace(
            "candidate",
            candidate_id,
            preferred_event_types=("candidate.promoted_to_observation",),
        )
        if self._trace_matches_event_type(promotion_trace, "candidate.promoted_to_observation"):
            result["promotion_trace"] = promotion_trace

        # Determine outcome based on traces found
        if result["promotion_trace"] is not None:
            result["outcome"] = "resolved_promoted"
            result["resolved"] = True
            # Extract promoted observation ID from the event and trace it
            if result["promotion_trace"].get("event"):
                promoted_obs_id = result["promotion_trace"]["event"].get("promoted_observation_id")
                if promoted_obs_id:
                    obs_trace = self.resolve_entity_trace(
                        "observation",
                        promoted_obs_id,
                        preferred_event_types=("observation.verified", "observation.proposed"),
                    )
                    result["promoted_observation_trace"] = obs_trace
        elif result["rejection_trace"] is not None:
            result["outcome"] = "rejected"
            result["resolved"] = True
        elif result["routed_trace"] is not None:
            result["outcome"] = "routed_to_verify"
            result["resolved"] = False
        elif result["proposed_trace"] is not None:
            result["outcome"] = "proposed"
            result["resolved"] = False

        return result

    @staticmethod
    def _trace_matches_event_type(trace: dict[str, Any] | None, expected_event_type: str) -> bool:
        """Check if a trace record contains the expected event type.

        Args:
            trace: A trace record from resolve_entity_trace, or None
            expected_event_type: The event type to match (e.g., "candidate.proposed")

        Returns:
            True if the trace exists and contains the expected event type
        """
        if trace is None:
            return False
        event = trace.get("event")
        if not isinstance(event, dict):
            return False
        return event.get("event_type") == expected_event_type

    def resolve_candidate_lineage(
        self,
        *,
        audit_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        """Reconstruct the full lineage from candidate to final finding.

        This traces the complete path:
        candidate → routed task(s) → promoted observation → verified observation → issue → finding

        Args:
            audit_id: The audit ID the candidate belongs to
            candidate_id: The candidate entity ID

        Returns:
            A lineage record containing:
            - candidate_id: The candidate identifier
            - audit_id: The parent audit ID
            - candidate_trace: Full candidate lifecycle trace
            - observation_lineage: Trace from promoted observation to verification
            - issue_lineage: Trace from verified observation to issue(s)
            - finding_lineage: Final findings that originated from this candidate
            - complete: True if the full lineage to findings is traceable
        """
        candidate_trace = self.resolve_candidate_trace(
            audit_id=audit_id,
            candidate_id=candidate_id,
        )

        result: dict[str, Any] = {
            "candidate_id": candidate_id,
            "audit_id": audit_id,
            "candidate_trace": candidate_trace,
            "observation_lineage": None,
            "issue_lineage": [],
            "finding_lineage": [],
            "complete": False,
        }

        # If candidate was promoted, trace the observation lineage
        if candidate_trace["outcome"] != "resolved_promoted":
            result["complete"] = candidate_trace["resolved"]
            return result

        promoted_obs_trace = candidate_trace.get("promoted_observation_trace")
        if promoted_obs_trace is None:
            return result

        observation_id = promoted_obs_trace.get("event", {}).get("entity_id")
        if not observation_id:
            return result

        result["observation_lineage"] = {
            "observation_id": observation_id,
            "trace": promoted_obs_trace,
        }

        # Find issues that reference this observation
        # This requires scanning worker_execution entries for issue.proposed events
        # that include this observation_id in their evidence
        issues_referencing_obs = self._find_issues_referencing_observation(
            audit_id=audit_id,
            observation_id=observation_id,
        )

        result["issue_lineage"] = issues_referencing_obs

        # Build finding lineage from issues
        result["finding_lineage"] = [
            {
                "issue_id": issue_ref["issue_id"],
                "issue_trace": issue_ref["issue_trace"],
                "observation_contribution": {
                    "observation_id": observation_id,
                    "is_primary_evidence": observation_id in issue_ref.get("evidence_observation_ids", []),
                },
            }
            for issue_ref in issues_referencing_obs
        ]

        # Lineage is complete if we have at least one issue trace
        result["complete"] = len(issues_referencing_obs) > 0

        return result

    def _find_issues_referencing_observation(
        self,
        *,
        audit_id: str,
        observation_id: str,
    ) -> list[dict[str, Any]]:
        """Find all issues that reference a specific observation in their evidence.

        Args:
            audit_id: The audit ID to search within
            observation_id: The observation ID to look for in issue evidence

        Returns:
            List of issue references with their traces
        """
        issues_found: list[dict[str, Any]] = []
        seen_issue_ids: set[str] = set()

        for entry in self.read_entries():
            if entry.get("entry_type") != "worker_execution":
                continue
            if entry.get("audit_id") != audit_id:
                continue

            for event_ref in entry.get("accepted_events", []):
                if event_ref.get("event_type") != "issue.proposed":
                    continue

                issue_id = event_ref.get("entity_id")
                if not issue_id or issue_id in seen_issue_ids:
                    continue

                # Check if this observation is in the evidence
                evidence_obs_ids = event_ref.get("evidence_observation_ids", [])
                if not isinstance(evidence_obs_ids, list):
                    continue

                if observation_id not in evidence_obs_ids:
                    continue

                seen_issue_ids.add(issue_id)
                issue_trace = self.resolve_entity_trace(
                    "issue",
                    issue_id,
                    preferred_event_types=("issue.proposed",),
                )

                issues_found.append({
                    "issue_id": issue_id,
                    "issue_trace": issue_trace,
                    "evidence_observation_ids": evidence_obs_ids,
                })

        return issues_found

    def list_candidates_by_outcome(
        self,
        *,
        audit_id: str,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        """List candidates filtered by their outcome status.

        Args:
            audit_id: The audit ID to search within
            outcome: Optional filter for candidate outcome. One of:
                - "proposed": Candidates still in proposed state
                - "routed_to_verify": Candidates routed for verification
                - "rejected": Candidates that were rejected
                - "resolved_promoted": Candidates promoted to observations
                - None: Return all candidates

        Returns:
            List of candidate summary records with:
            - candidate_id: The candidate identifier
            - candidate_type: The candidate type (risk_candidate, etc.)
            - outcome: Current outcome status
            - proposed_event_id: ID of the candidate.proposed event
            - trace_available: True if trace can be resolved
        """
        if outcome is not None and outcome not in {
            "proposed",
            "routed_to_verify",
            "rejected",
            "resolved_promoted",
        }:
            raise RunLedgerError(
                "outcome must be one of: proposed, routed_to_verify, rejected, resolved_promoted, or None."
            )

        candidates: dict[str, dict[str, Any]] = {}

        # First pass: collect all candidate.proposed events
        for entry in self.read_entries():
            if entry.get("entry_type") != "worker_execution":
                continue
            if entry.get("audit_id") != audit_id:
                continue

            for event_ref in entry.get("accepted_events", []):
                if event_ref.get("event_type") != "candidate.proposed":
                    continue

                candidate_id = event_ref.get("entity_id")
                if not candidate_id or candidate_id in candidates:
                    continue

                # Extract candidate_type from event if available
                candidate_type = event_ref.get("candidate_type", "unknown")

                candidates[candidate_id] = {
                    "candidate_id": candidate_id,
                    "candidate_type": candidate_type,
                    "outcome": "proposed",
                    "proposed_event_id": event_ref.get("event_id"),
                    "trace_available": True,
                }

        # Second pass: update outcomes based on subsequent events
        for entry in self.read_entries():
            if entry.get("entry_type") != "worker_execution":
                continue
            if entry.get("audit_id") != audit_id:
                continue

            for event_ref in entry.get("accepted_events", []):
                event_type = event_ref.get("event_type")
                candidate_id = event_ref.get("entity_id")

                if not candidate_id or candidate_id not in candidates:
                    continue

                if event_type == "candidate.routed_to_verify":
                    candidates[candidate_id]["outcome"] = "routed_to_verify"
                elif event_type == "candidate.rejected":
                    candidates[candidate_id]["outcome"] = "rejected"
                elif event_type == "candidate.promoted_to_observation":
                    candidates[candidate_id]["outcome"] = "resolved_promoted"
                    # Store the promoted observation ID
                    promoted_obs_id = event_ref.get("promoted_observation_id")
                    if promoted_obs_id:
                        candidates[candidate_id]["promoted_observation_id"] = promoted_obs_id

        # Filter by outcome if specified
        if outcome is not None:
            return [
                candidate
                for candidate in candidates.values()
                if candidate["outcome"] == outcome
            ]

        return list(candidates.values())

    def resolve_candidate_forensic_trace(
        self,
        *,
        audit_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        """Resolve complete forensic trace including all event outcomes for a candidate.

        This provides the most detailed trace, including rejected events
        that may have been attempted for this candidate.

        Args:
            audit_id: The audit ID the candidate belongs to
            candidate_id: The candidate entity ID

        Returns:
            A forensic trace record containing:
            - candidate_id: The candidate identifier
            - audit_id: The parent audit ID
            - all_events: All events (accepted and rejected) for this candidate
            - acceptance_rate: Ratio of accepted to total events
        """
        all_events: list[dict[str, Any]] = []
        accepted_count = 0
        rejected_count = 0

        for entry in self.read_entries():
            if entry.get("entry_type") != "worker_execution":
                continue
            if entry.get("audit_id") != audit_id:
                continue

            # Check accepted events
            for event_ref in entry.get("accepted_events", []):
                if event_ref.get("entity_type") != "candidate":
                    continue
                if event_ref.get("entity_id") != candidate_id:
                    continue

                all_events.append({
                    "event_id": event_ref.get("event_id"),
                    "event_type": event_ref.get("event_type"),
                    "outcome": "accepted",
                    "sequence": entry.get("sequence"),
                    "run_id": entry.get("run_id"),
                    "task_id": entry.get("task_id"),
                    "worker_role": entry.get("worker_role"),
                })
                accepted_count += 1

            # Check rejected events
            for event_ref in entry.get("rejected_events", []):
                if event_ref.get("entity_type") != "candidate":
                    continue
                if event_ref.get("entity_id") != candidate_id:
                    continue

                all_events.append({
                    "event_id": event_ref.get("event_id"),
                    "event_type": event_ref.get("event_type"),
                    "outcome": "rejected",
                    "issue_codes": event_ref.get("issue_codes", []),
                    "sequence": entry.get("sequence"),
                    "run_id": entry.get("run_id"),
                    "task_id": entry.get("task_id"),
                    "worker_role": entry.get("worker_role"),
                })
                rejected_count += 1

        total_events = accepted_count + rejected_count
        acceptance_rate = accepted_count / total_events if total_events > 0 else 0.0

        return {
            "candidate_id": candidate_id,
            "audit_id": audit_id,
            "all_events": sorted(all_events, key=lambda e: e.get("sequence", 0)),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "acceptance_rate": acceptance_rate,
        }

    @staticmethod
    def _normalize_event_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(outcome, dict):
            raise RunLedgerError("event_outcomes must contain JSON objects.")
        normalized = {
            "event_id": _optional_string(outcome.get("event_id")),
            "event_type": _optional_string(outcome.get("event_type")),
            "entity_type": _optional_string(outcome.get("entity_type")),
            "entity_id": _optional_string(outcome.get("entity_id")),
            "outcome": _optional_string(outcome.get("outcome")),
            "issue_codes": sorted(
                {
                    issue_code
                    for issue_code in outcome.get("issue_codes", [])
                    if isinstance(issue_code, str) and issue_code
                }
            ),
            "ledger_line_number": outcome.get("ledger_line_number"),
            "ledger_path": _optional_string(outcome.get("ledger_path")),
        }
        # Preserve structured rejection classification if present
        rejection = outcome.get("rejection")
        if isinstance(rejection, dict):
            normalized["rejection"] = rejection
        if normalized["outcome"] not in {"accepted", "rejected"}:
            raise RunLedgerError("event_outcomes must declare outcome 'accepted' or 'rejected'.")
        line_number = normalized["ledger_line_number"]
        if line_number is not None and (not isinstance(line_number, int) or line_number < 1):
            raise RunLedgerError("ledger_line_number must be a positive integer when present.")
        return normalized

    @staticmethod
    def _build_trace_record(
        entry: dict[str, Any],
        event_ref: dict[str, Any],
        *,
        processing_outcome: str,
    ) -> dict[str, Any]:
        return {
            "entry_id": entry["entry_id"],
            "sequence": entry["sequence"],
            "run_id": entry["run_id"],
            "audit_id": entry["audit_id"],
            "task_id": entry["task_id"],
            "slice_id": entry["slice_id"],
            "slice_fingerprint": entry.get("slice_fingerprint"),
            "worker_role": entry["worker_role"],
            "snapshot_ref": entry.get("snapshot_ref"),
            "input_digest": entry["input_digest"],
            "output_digest": entry["output_digest"],
            "prompt_digest": entry.get("prompt_digest"),
            "raw_output_digest": entry.get("raw_output_digest"),
            "adapter_invocation": _normalize(entry.get("adapter_invocation", {})),
            "execution_status": entry.get("execution_status"),
            "failure_stage": entry.get("failure_stage"),
            "processing_outcome": processing_outcome,
            "event": _normalize(event_ref),
        }

    @staticmethod
    def _next_run_id(entries: list[dict[str, Any]]) -> str:
        sequences = [
            int(entry["run_id"].split("_", 1)[1])
            for entry in entries
            if entry.get("entry_type") == "run_started"
            and isinstance(entry.get("run_id"), str)
            and entry["run_id"].startswith("run_")
            and entry["run_id"].split("_", 1)[1].isdigit()
        ]
        return f"run_{max(sequences, default=0) + 1:04d}"

    @staticmethod
    def _next_sequence(entries: list[dict[str, Any]]) -> int:
        sequences = [
            entry.get("sequence", 0)
            for entry in entries
            if isinstance(entry.get("sequence"), int)
        ]
        return max(sequences, default=0) + 1

    @staticmethod
    def _entry_id(sequence: int) -> str:
        return f"trace_{sequence:08d}"

    def _append_entry(self, entry: dict[str, Any]) -> None:
        # Redact secrets before persistence
        redacted_entry = redact_trace_entry(entry)
        serialized = _canonical_json(redacted_entry) + "\n"
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
