from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from runtime.evidence import ALLOWED_FINDING_EVIDENCE_CLASSES, EVIDENCE_CLASSES


ISSUE_COMPOSER_ROLE = "IssueComposer"
ISSUE_COMPOSER_ALLOWED_OUTPUTS = {
    "candidate_event_types": [
        "issue.proposed",
    ]
}
ISSUE_COMPOSER_FORBIDDEN_OUTPUTS = {
    "candidate_event_types": [
        "contradiction.registered",
        "hypothesis.proposed",
        "observation.proposed",
        "observation.rejected",
        "observation.verified",
        "question.opened",
    ],
    "actions": [
        "use_unverified_observations_as_facts",
        "unsupported_claims",
    ],
}
ISSUE_COMPOSER_CONSTRAINTS = {
    "context_source": "canonical_state_plus_task_only",
    "conversational_context_allowed": False,
    "unstored_context_allowed": False,
    "full_state_injection_allowed": False,
    "structured_output_required": True,
    "prose_state_mutation_allowed": False,
    "facts_require_source_binding": True,
    "implicit_guessing_allowed": False,
    "uncertainty_expression": "emit_hypothesis_or_question_instead_of_guessing",
}
ALLOWED_ISSUE_EVIDENCE_CLASSES = tuple(
    evidence_class
    for evidence_class in EVIDENCE_CLASSES
    if evidence_class in ALLOWED_FINDING_EVIDENCE_CLASSES
)


class IssueComposerWorkerError(Exception):
    """Base error for IssueComposer worker failures."""


class IssueComposerInputError(IssueComposerWorkerError):
    """Raised when IssueComposer input cannot be loaded or validated."""


class IssueComposerOutputError(IssueComposerWorkerError):
    """Raised when IssueComposer output cannot be parsed or validated."""


@dataclass(frozen=True)
class PreparedIssueComposerRequest:
    worker_input: dict[str, Any]
    prompt: str


@dataclass(frozen=True)
class IssueComposerResult:
    payload: dict[str, Any]
    candidate_events: list[dict[str, Any]]


class IssueComposerWorker:
    """Prepare IssueComposer input and validate structured issue proposal output."""

    def __init__(
        self,
        root_dir: str | Path,
        prompt_path: str | Path = "prompts/issue_composer.md",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.prompt_path = (self.root_dir / prompt_path).resolve()
        self.schema_dir = (self.root_dir / "schema").resolve()
        self._input_validator, self._output_validator = self._build_validators()

    def load_worker_input(self, source: str | Path | dict[str, Any]) -> dict[str, Any]:
        raw_payload = self._load_json_object(
            source,
            error_cls=IssueComposerInputError,
            label="IssueComposer input",
        )
        worker_input = self._coerce_to_issue_composer_contract(raw_payload)
        self._validate_worker_input(worker_input)
        self._validate_target_observation(worker_input)
        return worker_input

    def prepare_request(self, source: str | Path | dict[str, Any]) -> PreparedIssueComposerRequest:
        worker_input = self.load_worker_input(source)
        prompt = self.render_prompt(worker_input)
        return PreparedIssueComposerRequest(worker_input=worker_input, prompt=prompt)

    def render_prompt(self, worker_input: dict[str, Any]) -> str:
        if not self.prompt_path.exists():
            raise IssueComposerWorkerError(
                f"IssueComposer prompt file does not exist: {self.prompt_path}"
            )

        prompt_template = self.prompt_path.read_text(encoding="utf-8")
        serialized_input = json.dumps(worker_input, ensure_ascii=True, sort_keys=True, indent=2)
        return (
            prompt_template.rstrip()
            + "\n\nWorker Input JSON:\n"
            + "WORKER_INPUT_JSON_BEGIN\n"
            + serialized_input
            + "\nWORKER_INPUT_JSON_END\n"
        )

    def parse_output(
        self,
        source: str | Path | dict[str, Any],
        *,
        worker_input: str | Path | dict[str, Any] | None = None,
    ) -> IssueComposerResult:
        expected_input = self.load_worker_input(worker_input) if worker_input is not None else None
        payload = self._load_json_object(
            source,
            error_cls=IssueComposerOutputError,
            label="IssueComposer output",
        )
        self._validate_worker_output(payload)

        if expected_input is not None:
            self._validate_output_binding(payload, expected_input)
            self._validate_issue_evidence(payload, expected_input)

        candidate_events = self._normalize_candidate_events(payload["candidate_events"])
        normalized_payload = {
            "schema_version": payload["schema_version"],
            "slice_id": payload["slice_id"],
            "worker_role": payload["worker_role"],
            "task_id": payload["task_id"],
            "candidate_events": candidate_events,
        }
        return IssueComposerResult(payload=normalized_payload, candidate_events=candidate_events)

    @staticmethod
    def _load_json_object(
        source: str | Path | dict[str, Any],
        *,
        error_cls: type[Exception],
        label: str,
    ) -> dict[str, Any]:
        if isinstance(source, dict):
            return json.loads(json.dumps(source))

        if isinstance(source, Path):
            text = source.read_text(encoding="utf-8")
        elif isinstance(source, str):
            stripped = source.lstrip()
            if stripped.startswith("```"):
                raise error_cls(f"{label} must be raw JSON, not markdown or prose.")
            if stripped.startswith("{"):
                text = source
            else:
                path = Path(source)
                if not path.exists():
                    raise error_cls(f"{label} path does not exist: {path}")
                text = path.read_text(encoding="utf-8")
        else:
            raise error_cls(f"{label} must be provided as a dict, JSON string, or filesystem path.")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise error_cls(f"{label} must be valid JSON.") from exc

        if not isinstance(payload, dict):
            raise error_cls(f"{label} must decode to a JSON object.")
        return payload

    def _coerce_to_issue_composer_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_role = payload.get("worker_role")
        if worker_role != ISSUE_COMPOSER_ROLE:
            raise IssueComposerInputError(
                "IssueComposer worker requires worker_role "
                f"'{ISSUE_COMPOSER_ROLE}', received '{worker_role}'."
            )

        if "allowed_outputs" in payload and "forbidden_outputs" in payload:
            normalized = json.loads(json.dumps(payload))
            normalized.setdefault("answered_questions", {})
            return normalized

        required_slice_fields = {
            "schema_version",
            "slice_id",
            "worker_role",
            "task",
            "snapshot_ref",
            "target_paths",
            "relevant_observations",
            "open_questions",
            "constraints",
        }
        if not required_slice_fields.issubset(payload):
            missing = ", ".join(sorted(required_slice_fields - set(payload.keys())))
            raise IssueComposerInputError(f"IssueComposer slice is missing required fields: {missing}")

        return {
            "schema_version": "1.0.0",
            "slice_id": payload["slice_id"],
            "worker_role": ISSUE_COMPOSER_ROLE,
            "task": json.loads(json.dumps(payload["task"])),
            "snapshot_ref": payload["snapshot_ref"],
            "target_paths": json.loads(json.dumps(payload["target_paths"])),
            "relevant_observations": json.loads(json.dumps(payload["relevant_observations"])),
            "open_questions": json.loads(json.dumps(payload["open_questions"])),
            "answered_questions": json.loads(json.dumps(payload.get("answered_questions", {}))),
            "constraints": dict(ISSUE_COMPOSER_CONSTRAINTS),
            "allowed_outputs": json.loads(json.dumps(ISSUE_COMPOSER_ALLOWED_OUTPUTS)),
            "forbidden_outputs": json.loads(json.dumps(ISSUE_COMPOSER_FORBIDDEN_OUTPUTS)),
        }

    def _validate_worker_input(self, worker_input: dict[str, Any]) -> None:
        errors = sorted(
            self._input_validator.iter_errors(worker_input),
            key=lambda error: self._format_error_path(error),
        )
        if errors:
            formatted = "; ".join(self._format_validation_error(error) for error in errors)
            raise IssueComposerInputError(f"IssueComposer worker input is invalid: {formatted}")

    @staticmethod
    def _validate_target_observation(worker_input: dict[str, Any]) -> None:
        target_observation_id = worker_input["task"]["target"]["value"]
        relevant_observations = worker_input["relevant_observations"]
        observation = relevant_observations.get(target_observation_id)
        if observation is None:
            raise IssueComposerInputError(
                f"IssueComposer slice must include the target observation '{target_observation_id}'."
            )
        if observation.get("id") != target_observation_id:
            raise IssueComposerInputError(
                f"Target observation entry '{target_observation_id}' must contain matching payload id."
            )
        if observation.get("status") != "verified":
            raise IssueComposerInputError(
                f"IssueComposer target observation '{target_observation_id}' must be verified."
            )
        evidence_class = observation.get("evidence_class")
        if evidence_class not in ALLOWED_FINDING_EVIDENCE_CLASSES:
            allowed = ", ".join(ALLOWED_ISSUE_EVIDENCE_CLASSES)
            raise IssueComposerInputError(
                f"IssueComposer target observation '{target_observation_id}' must use one of the "
                f"allowed evidence classes: {allowed}. Received '{evidence_class}'."
            )

    def _validate_worker_output(self, payload: dict[str, Any]) -> None:
        errors = sorted(
            self._output_validator.iter_errors(payload),
            key=lambda error: self._format_error_path(error),
        )
        if errors:
            formatted = "; ".join(self._format_validation_error(error) for error in errors)
            raise IssueComposerOutputError(f"IssueComposer worker output is invalid: {formatted}")

    @staticmethod
    def _validate_output_binding(payload: dict[str, Any], worker_input: dict[str, Any]) -> None:
        if payload["worker_role"] != worker_input["worker_role"]:
            raise IssueComposerOutputError(
                f"IssueComposer output worker_role '{payload['worker_role']}' does not match "
                f"input worker_role '{worker_input['worker_role']}'."
            )
        if payload["slice_id"] != worker_input["slice_id"]:
            raise IssueComposerOutputError(
                f"IssueComposer output slice_id '{payload['slice_id']}' does not match "
                f"input slice_id '{worker_input['slice_id']}'."
            )
        expected_task_id = worker_input["task"]["id"]
        if payload["task_id"] != expected_task_id:
            raise IssueComposerOutputError(
                f"IssueComposer output task_id '{payload['task_id']}' does not match "
                f"input task_id '{expected_task_id}'."
            )

    @staticmethod
    def _validate_issue_evidence(payload: dict[str, Any], worker_input: dict[str, Any]) -> None:
        target_observation_id = worker_input["task"]["target"]["value"]
        relevant_observations = worker_input["relevant_observations"]
        verified_observation_ids = set(relevant_observations)
        open_question_ids = set(worker_input["open_questions"])
        answered_question_ids = set(worker_input.get("answered_questions", {}))
        allowed_question_ids = open_question_ids | answered_question_ids

        for event in payload["candidate_events"]:
            evidence = event["payload"]["evidence"]
            observation_ids = set(evidence["observation_ids"])
            if not observation_ids:
                raise IssueComposerOutputError(
                    f"Issue '{event['entity_id']}' must reference at least one verified observation."
                )
            if target_observation_id not in observation_ids:
                raise IssueComposerOutputError(
                    f"Issue '{event['entity_id']}' must reference target observation "
                    f"'{target_observation_id}'."
                )
            unsupported_observation_ids = sorted(observation_ids - verified_observation_ids)
            if unsupported_observation_ids:
                joined = ", ".join(unsupported_observation_ids)
                raise IssueComposerOutputError(
                    f"Issue '{event['entity_id']}' references observations not present as verified "
                    f"evidence in the input slice: {joined}."
                )

            disallowed_evidence_observations = sorted(
                observation_id
                for observation_id in observation_ids
                if relevant_observations[observation_id].get("evidence_class")
                not in ALLOWED_FINDING_EVIDENCE_CLASSES
            )
            if disallowed_evidence_observations:
                joined = ", ".join(disallowed_evidence_observations)
                allowed = ", ".join(ALLOWED_ISSUE_EVIDENCE_CLASSES)
                raise IssueComposerOutputError(
                    f"Issue '{event['entity_id']}' references observations whose evidence_class is "
                    f"not allowed for findings: {joined}. Allowed classes: {allowed}."
                )

            question_ids = set(evidence.get("question_ids", []))
            unsupported_question_ids = sorted(question_ids - allowed_question_ids)
            if unsupported_question_ids:
                joined = ", ".join(unsupported_question_ids)
                raise IssueComposerOutputError(
                    f"Issue '{event['entity_id']}' references questions not present in the input "
                    f"slice: {joined}."
                )

            if open_question_ids and not open_question_ids.issubset(question_ids):
                missing = ", ".join(sorted(open_question_ids - question_ids))
                raise IssueComposerOutputError(
                    f"Issue '{event['entity_id']}' must preserve unanswered questions as uncertainty. "
                    f"Missing question_ids: {missing}."
                )

    @staticmethod
    def _normalize_candidate_events(candidate_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_events = [json.loads(json.dumps(event)) for event in candidate_events]
        return sorted(
            normalized_events,
            key=lambda event: (
                event["event_type"],
                event["entity_type"],
                event["entity_id"],
                event["id"],
            ),
        )

    def _build_validators(self) -> tuple[Draft202012Validator, Draft202012Validator]:
        schema_names = (
            "audit.schema.json",
            "event.schema.json",
            "worker_input.schema.json",
            "worker_output.schema.json",
        )
        schemas: dict[str, dict[str, Any]] = {}

        for schema_name in schema_names:
            schema_path = self.schema_dir / schema_name
            with schema_path.open("r", encoding="utf-8") as handle:
                schema = json.load(handle)
            Draft202012Validator.check_schema(schema)
            schemas[schema_name] = schema

        registry = Registry().with_resources(
            [
                (schema["$id"], Resource.from_contents(schema))
                for schema in schemas.values()
            ]
        )
        return (
            Draft202012Validator(schemas["worker_input.schema.json"], registry=registry),
            Draft202012Validator(schemas["worker_output.schema.json"], registry=registry),
        )

    @staticmethod
    def _format_error_path(error: ValidationError) -> str:
        return ".".join(str(part) for part in error.absolute_path)

    @classmethod
    def _format_validation_error(cls, error: ValidationError) -> str:
        path = cls._format_error_path(error)
        if path:
            return f"{path}: {error.message}"
        return error.message
