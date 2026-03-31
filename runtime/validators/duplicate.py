from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime.canonicalization import build_event_fingerprint, canonical_json, semantic_equivalent
from runtime.event_store import EventStore
from runtime.validators.models import ValidationIssue


class DuplicateValidator:
    name = "duplicate"

    def __init__(self, root_dir: str | Path, events_dir: str | Path = "events") -> None:
        self.event_store = EventStore(root_dir, events_dir=events_dir)

    def validate(self, event: dict[str, Any]) -> list[ValidationIssue]:
        candidate_serialized = self._serialize_event(event)
        candidate_fingerprint = build_event_fingerprint(event)

        for stored_event in self.event_store.iter_stored_events():
            existing = stored_event.event
            existing_serialized = self._serialize_event(existing)
            existing_fingerprint = build_event_fingerprint(existing)

            if existing["id"] == event["id"]:
                if existing_serialized == candidate_serialized or semantic_equivalent(existing, event):
                    return [
                        ValidationIssue(
                            validator=self.name,
                            code="duplicate_submission",
                            message=f"Event '{event['id']}' already exists in the ledger.",
                            path="id",
                        )
                    ]
                return [
                    ValidationIssue(
                        validator=self.name,
                        code="event_id_conflict",
                        message=f"Event id '{event['id']}' already exists with different content.",
                        path="id",
                    )
                ]

            if (
                existing["audit_id"] == event["audit_id"]
                and existing["event_type"] == event["event_type"]
                and existing_fingerprint == candidate_fingerprint
            ):
                return [
                    ValidationIssue(
                        validator=self.name,
                        code="duplicate_submission",
                        message=(
                            "A semantically equivalent event already exists in the ledger for audit "
                            f"'{event['audit_id']}'."
                        ),
                        path="payload",
                    )
                ]

            same_audit = existing["audit_id"] == event["audit_id"]
            same_idempotency_key = existing["idempotency_key"] == event["idempotency_key"]
            if same_audit and same_idempotency_key:
                if existing_serialized == candidate_serialized or semantic_equivalent(existing, event):
                    return [
                        ValidationIssue(
                            validator=self.name,
                            code="duplicate_submission",
                            message=(
                                "Idempotency key "
                                f"'{event['idempotency_key']}' already exists for audit "
                                f"'{event['audit_id']}'."
                            ),
                            path="idempotency_key",
                        )
                    ]
                return [
                    ValidationIssue(
                        validator=self.name,
                        code="idempotency_conflict",
                        message=(
                            "Idempotency key "
                            f"'{event['idempotency_key']}' already exists for audit "
                            f"'{event['audit_id']}' with different content."
                        ),
                        path="idempotency_key",
                    )
                ]

        return []

    @staticmethod
    def _serialize_event(event: dict[str, Any]) -> str:
        return canonical_json(json.loads(json.dumps(event)))
