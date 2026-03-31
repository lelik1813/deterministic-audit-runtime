from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from runtime.event_store import EventStore
from runtime.projector import StateProjector
from runtime.validators.models import ValidationIssue


class TransitionValidator:
    name = "transition"

    def __init__(self, root_dir: str | Path, events_dir: str | Path = "events") -> None:
        self.root_dir = Path(root_dir).resolve()
        self.event_store = EventStore(self.root_dir, events_dir=events_dir)
        self.projector = StateProjector(self.root_dir, events_dir=events_dir)
        self.rules = self._load_rules()

    def validate(self, event: dict[str, Any]) -> list[ValidationIssue]:
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

        # Augment state with candidate layer (non-authoritative, for transition validation only)
        # Candidates are NOT merged into canonical truth; they exist in a separate layer.
        current_state["candidates"] = self._build_candidate_state(event["audit_id"])

        issues: list[ValidationIssue] = []
        issues.extend(self._validate_worker_permissions(event))
        issues.extend(self._validate_transition_matrix(event, current_state))
        issues.extend(self._validate_cross_entity_guards(event, current_state))
        return issues

    def _build_candidate_state(self, audit_id: str) -> dict[str, Any]:
        """Build candidate state from accepted events for validation purposes.

        Candidates are non-authoritative and NOT part of canonical truth.
        This method builds a separate candidate layer for transition validation only.
        """
        candidates: dict[str, Any] = {}
        for stored_event in self.event_store.iter_stored_events(audit_id=audit_id):
            event = stored_event.event
            if event.get("acceptance", {}).get("status") != "accepted":
                continue
            if event.get("entity_type") != "candidate":
                continue

            candidate_id = event.get("entity_id")
            if candidate_id is None:
                continue

            payload = event.get("payload", {})
            # Apply status from payload
            if "status" in payload:
                candidates[candidate_id] = payload

        return candidates

    def _validate_worker_permissions(self, event: dict[str, Any]) -> list[ValidationIssue]:
        actor = event["actor"]
        if actor["actor_type"] != "worker":
            return []

        role = actor.get("role")
        if role is None:
            return [
                ValidationIssue(
                    validator=self.name,
                    code="missing_worker_role",
                    message="Worker events must include a non-null role.",
                    path="actor.role",
                )
            ]

        allowed = self.rules["worker_role_event_permissions"].get(role)
        if allowed is None or event["event_type"] not in allowed["allowed_event_types"]:
            return [
                ValidationIssue(
                    validator=self.name,
                    code="worker_event_forbidden",
                    message=f"Worker role '{role}' cannot emit event type '{event['event_type']}'.",
                    path="event_type",
                )
            ]
        return []

    def _validate_transition_matrix(
        self,
        event: dict[str, Any],
        current_state: dict[str, Any],
    ) -> list[ValidationIssue]:
        entity_rules = self.rules["entities"][event["entity_type"]]
        current_state_value = self._current_state(
            entity_type=event["entity_type"],
            entity_id=event["entity_id"],
            state=current_state,
        )
        requested_state = self._requested_state(event)

        allowed_rule = self._find_allowed_rule(
            entity_rules=entity_rules,
            current_state=current_state_value,
            requested_state=requested_state,
            event_type=event["event_type"],
        )
        if allowed_rule is not None:
            return []

        forbidden_rule = self._find_forbidden_rule(
            entity_rules=entity_rules,
            current_state=current_state_value,
            requested_state=requested_state,
        )
        if forbidden_rule is not None:
            return [
                ValidationIssue(
                    validator=self.name,
                    code="invalid_transition",
                    message=(
                        f"Transition '{current_state_value} -> {requested_state}' is forbidden for "
                        f"entity type '{event['entity_type']}' via event '{event['event_type']}'."
                    ),
                    rule_id=forbidden_rule["rule_id"],
                    path="payload.status" if event["entity_type"] != "decision" else None,
                )
            ]

        return [
            ValidationIssue(
                validator=self.name,
                code="invalid_transition",
                message=(
                    f"No allowed transition matches '{current_state_value} -> {requested_state}' for "
                    f"entity type '{event['entity_type']}' via event '{event['event_type']}'."
                ),
                path="payload.status" if event["entity_type"] != "decision" else None,
            )
        ]

    def _validate_cross_entity_guards(
        self,
        event: dict[str, Any],
        current_state: dict[str, Any],
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        for guard in self.rules["cross_entity_guards"]:
            guard_kind = guard["guard_kind"]

            if guard_kind == "referenced_entities_exist":
                if not self._guard_matches_event(guard, event):
                    continue
                referenced_ids = self._resolve_path(event, guard["field_path"]) or []
                for index, entity_id in enumerate(referenced_ids):
                    if not self._entity_exists(
                        current_state,
                        guard["referenced_entity_type"],
                        entity_id,
                    ):
                        issues.append(
                            ValidationIssue(
                                validator=self.name,
                                code="referenced_entity_missing",
                                message=(
                                    f"Referenced {guard['referenced_entity_type']} '{entity_id}' does not "
                                    "exist in accepted canonical state."
                                ),
                                rule_id=guard["rule_id"],
                                path=f"{guard['field_path']}[{index}]",
                            )
                        )

            if guard_kind == "referenced_entities_in_state":
                if not self._guard_matches_event(guard, event):
                    continue
                referenced_ids = self._resolve_path(event, guard["field_path"]) or []
                for index, entity_id in enumerate(referenced_ids):
                    entity = self._get_entity(
                        current_state,
                        guard["referenced_entity_type"],
                        entity_id,
                    )
                    if entity is None:
                        issues.append(
                            ValidationIssue(
                                validator=self.name,
                                code="referenced_entity_missing",
                                message=(
                                    f"Referenced {guard['referenced_entity_type']} '{entity_id}' does not "
                                    "exist in accepted canonical state."
                                ),
                                rule_id=guard["rule_id"],
                                path=f"{guard['field_path']}[{index}]",
                            )
                        )
                        continue
                    if entity.get("status") != guard["required_state"]:
                        issues.append(
                            ValidationIssue(
                                validator=self.name,
                                code="referenced_entity_wrong_state",
                                message=(
                                    f"Referenced {guard['referenced_entity_type']} '{entity_id}' must be "
                                    f"in state '{guard['required_state']}'."
                                ),
                                rule_id=guard["rule_id"],
                                path=f"{guard['field_path']}[{index}]",
                            )
                        )

            if guard_kind == "referenced_entities_field_in_set":
                if not self._guard_matches_event(guard, event):
                    continue
                referenced_ids = self._resolve_path(event, guard["field_path"]) or []
                for index, entity_id in enumerate(referenced_ids):
                    entity = self._get_entity(
                        current_state,
                        guard["referenced_entity_type"],
                        entity_id,
                    )
                    if entity is None:
                        issues.append(
                            ValidationIssue(
                                validator=self.name,
                                code="referenced_entity_missing",
                                message=(
                                    f"Referenced {guard['referenced_entity_type']} '{entity_id}' does not "
                                    "exist in accepted canonical state."
                                ),
                                rule_id=guard["rule_id"],
                                path=f"{guard['field_path']}[{index}]",
                            )
                        )
                        continue

                    field_value = self._resolve_path(entity, guard["referenced_field_path"])
                    allowed_values = guard.get("allowed_values", [])
                    if field_value not in allowed_values:
                        issues.append(
                            ValidationIssue(
                                validator=self.name,
                                code="referenced_entity_field_forbidden",
                                message=(
                                    f"Referenced {guard['referenced_entity_type']} '{entity_id}' must use one "
                                    f"of the allowed values for '{guard['referenced_field_path']}': "
                                    f"{', '.join(allowed_values)}."
                                ),
                                rule_id=guard["rule_id"],
                                path=f"{guard['field_path']}[{index}]",
                            )
                        )

            if guard_kind == "field_dependency":
                if not self._guard_matches_event(guard, event):
                    continue
                when_value = self._resolve_path(event, guard["when_field"]["path"])
                required_value = self._resolve_path(event, guard["requires_field"]["path"])
                if when_value is not None and required_value is None:
                    issues.append(
                        ValidationIssue(
                            validator=self.name,
                            code="field_dependency_failed",
                            message=(
                                f"Field '{guard['requires_field']['path']}' must be non-null when "
                                f"'{guard['when_field']['path']}' is non-null."
                            ),
                            rule_id=guard["rule_id"],
                            path=guard["requires_field"]["path"],
                        )
                    )

            if guard_kind == "forbidden_entity_promotion":
                if event["entity_type"] != guard["target_entity_type"]:
                    continue
                caused_by_event_id = event.get("caused_by_event_id")
                if caused_by_event_id is None:
                    continue
                caused_by_event = self.event_store.get_event(caused_by_event_id)
                if caused_by_event is None:
                    continue
                if caused_by_event["entity_type"] == guard["source_entity_type"]:
                    issues.append(
                        ValidationIssue(
                            validator=self.name,
                            code="forbidden_entity_promotion",
                            message=(
                                f"Direct promotion from {guard['source_entity_type']} to "
                                f"{guard['target_entity_type']} is forbidden."
                            ),
                            rule_id=guard["rule_id"],
                            path="caused_by_event_id",
                        )
                    )

            # Candidate promotion path validation (v1.2 Step 10)
            if guard_kind == "candidate_promotion_path_valid":
                if event["entity_type"] != "candidate":
                    continue
                if event["event_type"] != "candidate.promoted_to_observation":
                    continue

                payload = event.get("payload", {})
                candidate_id = event.get("entity_id")
                promoted_observation_id = payload.get("promoted_observation_id")

                # Check promoted_observation_id is present
                if promoted_observation_id is None:
                    issues.append(
                        ValidationIssue(
                            validator=self.name,
                            code="promotion_missing_observation_id",
                            message="Candidate promotion requires promoted_observation_id field.",
                            rule_id=guard["rule_id"],
                            path="payload.promoted_observation_id",
                        )
                    )
                    continue

                # Check candidate is in correct state (routed_to_verify)
                candidates = current_state.get("candidates", {})
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    issues.append(
                        ValidationIssue(
                            validator=self.name,
                            code="promotion_candidate_not_found",
                            message=f"Candidate '{candidate_id}' does not exist in state.",
                            rule_id=guard["rule_id"],
                            path="entity_id",
                        )
                    )
                    continue

                # Check observation exists
                observations = current_state.get("observations", {})
                observation = observations.get(promoted_observation_id)
                if observation is None:
                    issues.append(
                        ValidationIssue(
                            validator=self.name,
                            code="promotion_observation_not_found",
                            message=f"Promoted observation '{promoted_observation_id}' does not exist.",
                            rule_id=guard["rule_id"],
                            path="payload.promoted_observation_id",
                        )
                    )
                    continue

                # Check bidirectional link: observation.provenance.candidate_ref
                obs_candidate_ref = observation.get("provenance", {}).get("candidate_ref")
                if obs_candidate_ref != candidate_id:
                    issues.append(
                        ValidationIssue(
                            validator=self.name,
                            code="promotion_observation_link_mismatch",
                            message=(
                                f"Promoted observation '{promoted_observation_id}' "
                                f"provenance.candidate_ref '{obs_candidate_ref}' "
                                f"does not match candidate '{candidate_id}'."
                            ),
                            rule_id=guard["rule_id"],
                            path="payload.promoted_observation_id",
                        )
                    )

        return issues

    def _load_rules(self) -> dict[str, Any]:
        rules_path = self.root_dir / "rules" / "transition_rules.yaml"
        with rules_path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    @staticmethod
    def _find_allowed_rule(
        entity_rules: dict[str, Any],
        current_state: str,
        requested_state: str,
        event_type: str,
    ) -> dict[str, Any] | None:
        for rule in entity_rules["allowed_transitions"]:
            if rule["from"] != current_state or rule["to"] != requested_state:
                continue
            if event_type in rule.get("via_event_types", []):
                return rule
        return None

    @staticmethod
    def _find_forbidden_rule(
        entity_rules: dict[str, Any],
        current_state: str,
        requested_state: str,
    ) -> dict[str, Any] | None:
        for rule in entity_rules["forbidden_transitions"]:
            if rule["from"] != current_state:
                continue
            if rule["to"] == "*" or rule["to"] == requested_state:
                return rule
        return None

    @staticmethod
    def _requested_state(event: dict[str, Any]) -> str:
        if event["entity_type"] == "decision":
            return "recorded"
        return event["payload"]["status"]

    @staticmethod
    def _current_state(entity_type: str, entity_id: str, state: dict[str, Any]) -> str:
        if entity_type == "audit":
            audit = state["audit"]
            if audit is None or audit["id"] != entity_id:
                return "__absent__"
            return audit["status"]

        if entity_type == "decision":
            return "recorded" if entity_id in state["decisions"] else "__absent__"

        # Candidate entities are in a separate non-authoritative layer
        if entity_type == "candidate":
            candidates = state.get("candidates", {})
            candidate = candidates.get(entity_id)
            if candidate is None:
                return "__absent__"
            return candidate["status"]

        collection_name = StateProjector.ENTITY_COLLECTIONS[entity_type]
        entity = state[collection_name].get(entity_id)
        if entity is None:
            return "__absent__"
        return entity["status"]

    @staticmethod
    def _guard_matches_event(guard: dict[str, Any], event: dict[str, Any]) -> bool:
        if guard.get("entity_type") != event["entity_type"]:
            return False
        guard_event_type = guard.get("event_type")
        return guard_event_type == "*" or guard_event_type == event["event_type"]

    @staticmethod
    def _resolve_path(obj: dict[str, Any], path: str) -> Any:
        current: Any = obj
        for part in path.split("."):
            if current is None:
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _entity_exists(state: dict[str, Any], entity_type: str, entity_id: str) -> bool:
        return TransitionValidator._get_entity(state, entity_type, entity_id) is not None

    @staticmethod
    def _get_entity(state: dict[str, Any], entity_type: str, entity_id: str) -> dict[str, Any] | None:
        if entity_type == "audit":
            audit = state["audit"]
            if audit is not None and audit["id"] == entity_id:
                return audit
            return None

        if entity_type == "decision":
            return state["decisions"].get(entity_id)

        # Candidate entities are in a separate non-authoritative layer
        if entity_type == "candidate":
            candidates = state.get("candidates", {})
            return candidates.get(entity_id)

        collection_name = StateProjector.ENTITY_COLLECTIONS[entity_type]
        return state[collection_name].get(entity_id)
