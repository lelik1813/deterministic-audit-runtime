from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

try:
    from runtime.event_store import EventStore
    from runtime.secret_redaction import redact_canonical_state
except ModuleNotFoundError:  # pragma: no cover - allows direct script execution.
    from event_store import EventStore
    from secret_redaction import redact_canonical_state


class ProjectionError(Exception):
    """Raised when canonical state cannot be projected deterministically."""


@dataclass(frozen=True)
class ProjectionResult:
    audit_id: str | None
    total_events: int
    accepted_events: int
    applied_events: int
    projection_id: str
    canonical_state_path: Path
    snapshot_path: Path


class StateProjector:
    """Rebuild canonical state from accepted events in append order."""

    ENTITY_COLLECTIONS = {
        "task": "tasks",
        "observation": "observations",
        "hypothesis": "hypotheses",
        "issue": "issues",
        "question": "questions",
        "contradiction": "contradictions",
        "decision": "decisions",
        "candidate": "candidates",  # Non-authoritative candidate layer (v1.2)
    }

    def __init__(
        self,
        root_dir: str | Path,
        events_dir: str | Path = "events",
        state_dir: str | Path = "state",
        canonical_state_name: str = "canonical_state.json",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.state_dir = (self.root_dir / state_dir).resolve()
        self.projections_dir = (self.state_dir / "projections").resolve()
        self.canonical_state_path = (self.state_dir / canonical_state_name).resolve()
        self.schema_path = (self.root_dir / "schema" / "audit.schema.json").resolve()

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.projections_dir.mkdir(parents=True, exist_ok=True)
        self.event_store = EventStore(self.root_dir, events_dir=events_dir)
        self._validator = self._build_state_validator()

    def build_state(self, audit_id: str | None = None) -> tuple[dict[str, Any], int, int]:
        stored_events = list(self.event_store.iter_stored_events(audit_id=audit_id))
        accepted_events = [
            stored_event.event
            for stored_event in stored_events
            if stored_event.event["acceptance"]["status"] == "accepted"
        ]

        resolved_audit_id = self._resolve_audit_id(accepted_events, requested_audit_id=audit_id)
        state = self._empty_state()
        applied_events = 0

        for event in accepted_events:
            if resolved_audit_id is not None and event["audit_id"] != resolved_audit_id:
                continue
            self._apply_event(state, event)
            applied_events += 1

        if applied_events > 0 and state["audit"] is None:
            raise ProjectionError("Accepted events exist, but no accepted audit root was projected.")

        self._validate_state(state)
        return state, len(stored_events), len(accepted_events)

    def write_projection(self, audit_id: str | None = None) -> ProjectionResult:
        state, total_events, accepted_events = self.build_state(audit_id=audit_id)
        # Redact secrets before serialization
        redacted_state = redact_canonical_state(state)
        compact_json = self._serialize_compact(redacted_state)
        pretty_json = self._serialize_pretty(redacted_state)
        projection_id = hashlib.sha256(compact_json.encode("ascii")).hexdigest()[:16]

        self.canonical_state_path.write_text(pretty_json, encoding="utf-8", newline="\n")
        snapshot_path = self.projections_dir / f"canonical_state.{projection_id}.json"
        if not snapshot_path.exists():
            snapshot_path.write_text(pretty_json, encoding="utf-8", newline="\n")

        return ProjectionResult(
            audit_id=audit_id,
            total_events=total_events,
            accepted_events=accepted_events,
            applied_events=accepted_events,
            projection_id=projection_id,
            canonical_state_path=self.canonical_state_path,
            snapshot_path=snapshot_path,
        )

    def _apply_event(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        entity_type = event["entity_type"]
        entity_id = event["entity_id"]
        payload = self._normalize_payload(event["payload"])

        if payload["id"] != entity_id:
            raise ProjectionError(
                f"Event '{event['id']}' payload id '{payload['id']}' does not match entity id '{entity_id}'."
            )

        if entity_type == "audit":
            if payload["id"] != event["audit_id"]:
                raise ProjectionError(
                    f"Event '{event['id']}' payload audit id '{payload['id']}' "
                    f"does not match event audit id '{event['audit_id']}'."
                )
            state["audit"] = payload
            return

        payload_audit_id = payload.get("audit_id")
        if payload_audit_id != event["audit_id"]:
            raise ProjectionError(
                f"Event '{event['id']}' payload audit id '{payload_audit_id}' "
                f"does not match event audit id '{event['audit_id']}'."
            )

        collection_name = self.ENTITY_COLLECTIONS.get(entity_type)
        if collection_name is None:
            raise ProjectionError(f"Unsupported entity type '{entity_type}' in event '{event['id']}'.")

        state[collection_name][entity_id] = payload

    def _build_state_validator(self) -> Draft202012Validator:
        with self.schema_path.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def _validate_state(self, state: dict[str, Any]) -> None:
        errors = sorted(
            self._validator.iter_errors(state),
            key=lambda error: self._format_error_path(error),
        )
        if not errors:
            return

        formatted = "; ".join(self._format_validation_error(error) for error in errors)
        raise ProjectionError(f"Projected state is invalid: {formatted}")

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "audit": None,
            "tasks": {},
            "observations": {},
            "hypotheses": {},
            "issues": {},
            "questions": {},
            "contradictions": {},
            "decisions": {},
            "candidates": {},  # Non-authoritative candidate layer (v1.2)
        }

    @staticmethod
    def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(payload))

    @staticmethod
    def _resolve_audit_id(
        accepted_events: list[dict[str, Any]],
        requested_audit_id: str | None,
    ) -> str | None:
        if requested_audit_id is not None:
            return requested_audit_id

        audit_ids = sorted({event["audit_id"] for event in accepted_events})
        if len(audit_ids) > 1:
            raise ProjectionError(
                "Canonical state projection requires exactly one audit id when no audit_id filter "
                f"is provided, found: {', '.join(audit_ids)}"
            )
        return audit_ids[0] if audit_ids else None

    @staticmethod
    def _serialize_compact(state: dict[str, Any]) -> str:
        return json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _serialize_pretty(cls, state: dict[str, Any]) -> str:
        return json.dumps(state, ensure_ascii=True, sort_keys=True, indent=2) + "\n"

    @staticmethod
    def _format_error_path(error: ValidationError) -> str:
        return ".".join(str(part) for part in error.absolute_path)

    @classmethod
    def _format_validation_error(cls, error: ValidationError) -> str:
        path = cls._format_error_path(error)
        if path:
            return f"{path}: {error.message}"
        return error.message
