from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.projector import StateProjector
from runtime.validators.models import ValidationIssue


class ContradictionValidator:
    name = "contradiction"

    def __init__(self, root_dir: str | Path, events_dir: str | Path = "events") -> None:
        self.projector = StateProjector(root_dir, events_dir=events_dir)

    def validate(self, event: dict[str, Any]) -> list[ValidationIssue]:
        if event["event_type"] != "contradiction.registered":
            return []

        try:
            current_state, _, _ = self.projector.build_state(audit_id=event["audit_id"])
        except Exception as exc:  # pragma: no cover - deterministic path surfaced as issue.
            return [
                ValidationIssue(
                    validator=self.name,
                    code="state_projection_failed",
                    message=str(exc),
                )
            ]

        issues: list[ValidationIssue] = []
        for index, entity_ref in enumerate(event["payload"]["conflicting_entity_refs"]):
            if not self._entity_exists(current_state, entity_ref["entity_type"], entity_ref["entity_id"]):
                issues.append(
                    ValidationIssue(
                        validator=self.name,
                        code="contradiction_reference_missing",
                        message=(
                            "Contradiction references an entity that does not exist in accepted "
                            "canonical state."
                        ),
                        path=f"payload.conflicting_entity_refs[{index}]",
                    )
                )

        return issues

    @staticmethod
    def _entity_exists(state: dict[str, Any], entity_type: str, entity_id: str) -> bool:
        if entity_type == "audit":
            return state["audit"] is not None and state["audit"]["id"] == entity_id

        collection_name = f"{entity_type}s"
        if entity_type == "contradiction":
            collection_name = "contradictions"
        if entity_type == "hypothesis":
            collection_name = "hypotheses"
        return entity_id in state.get(collection_name, {})
