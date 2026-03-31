from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource


CANDIDATE_GENERATOR_ROLE = "CandidateGenerator"
CANDIDATE_GENERATOR_ALLOWED_OUTPUTS = {
    "candidate_event_types": [
        "candidate.proposed",
    ]
}
CANDIDATE_GENERATOR_FORBIDDEN_OUTPUTS = {
    "candidate_event_types": [
        "hypothesis.proposed",
        "issue.proposed",
        "observation.proposed",
        "observation.rejected",
        "observation.verified",
        "contradiction.registered",
        "question.opened",
    ],
    "actions": [
        "issue_creation",
        "observation_creation",
        "severity_assignment",
        "truth_claims",
        "direct_state_mutation",
    ],
}
CANDIDATE_GENERATOR_CONSTRAINTS = {
    "context_source": "canonical_state_plus_task_only",
    "conversational_context_allowed": False,
    "unstored_context_allowed": False,
    "full_state_injection_allowed": False,
    "structured_output_required": True,
    "prose_state_mutation_allowed": False,
    "candidates_are_proposals_only": True,
    "candidates_are_non_authoritative": True,
    "uncertainty_must_remain_explicit": True,
    "no_direct_path_to_issue_or_report": True,
}

# Candidate types that CandidateGenerator may produce
CANDIDATE_TYPES = (
    "risk_candidate",
    "policy_candidate",
    "cross_file_correlation",
    "verification_target",
)


class CandidateGeneratorWorkerError(Exception):
    """Base error for CandidateGenerator worker failures."""


class CandidateGeneratorInputError(CandidateGeneratorWorkerError):
    """Raised when CandidateGenerator input cannot be loaded or validated."""


class CandidateGeneratorOutputError(CandidateGeneratorWorkerError):
    """Raised when CandidateGenerator output cannot be parsed or validated."""


@dataclass(frozen=True)
class PreparedCandidateGeneratorRequest:
    worker_input: dict[str, Any]
    prompt: str


@dataclass(frozen=True)
class CandidateGeneratorResult:
    payload: dict[str, Any]
    candidate_events: list[dict[str, Any]]


class CandidateGeneratorWorker:
    """Prepare CandidateGenerator input and validate structured candidate output.

    The CandidateGenerator is a non-authoritative worker that expands recall by
    generating speculative candidates. Candidates are NOT truth-bearing entities
    and require verification before becoming authoritative.

    CRITICAL: This worker may ONLY emit candidate.proposed events. It cannot
    create observations, issues, or any truth-bearing output.
    """

    def __init__(
        self,
        root_dir: str | Path,
        prompt_path: str | Path = "prompts/candidate_generator.md",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.prompt_path = (self.root_dir / prompt_path).resolve()
        self.schema_dir = (self.root_dir / "schema").resolve()
        self._input_validator, self._output_validator = self._build_validators()

    def load_worker_input(self, source: str | Path | dict[str, Any]) -> dict[str, Any]:
        raw_payload = self._load_json_object(
            source,
            error_cls=CandidateGeneratorInputError,
            label="CandidateGenerator input",
        )
        worker_input = self._coerce_to_candidate_generator_contract(raw_payload)
        self._validate_worker_input(worker_input)
        return worker_input

    def prepare_request(self, source: str | Path | dict[str, Any]) -> PreparedCandidateGeneratorRequest:
        worker_input = self.load_worker_input(source)
        prompt = self.render_prompt(worker_input)
        return PreparedCandidateGeneratorRequest(worker_input=worker_input, prompt=prompt)

    def render_prompt(self, worker_input: dict[str, Any]) -> str:
        if not self.prompt_path.exists():
            raise CandidateGeneratorWorkerError(
                f"CandidateGenerator prompt file does not exist: {self.prompt_path}"
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
    ) -> CandidateGeneratorResult:
        expected_input = self.load_worker_input(worker_input) if worker_input is not None else None
        payload = self._load_json_object(
            source,
            error_cls=CandidateGeneratorOutputError,
            label="CandidateGenerator output",
        )
        self._validate_worker_output(payload)

        if expected_input is not None:
            self._validate_output_binding(payload, expected_input)

        self._validate_candidate_events_only(payload)

        candidate_events = self._normalize_candidate_events(payload["candidate_events"])
        normalized_payload = {
            "schema_version": payload["schema_version"],
            "slice_id": payload["slice_id"],
            "worker_role": payload["worker_role"],
            "task_id": payload["task_id"],
            "candidate_events": candidate_events,
        }
        return CandidateGeneratorResult(payload=normalized_payload, candidate_events=candidate_events)

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

    def _coerce_to_candidate_generator_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_role = payload.get("worker_role")
        if worker_role != CANDIDATE_GENERATOR_ROLE:
            raise CandidateGeneratorInputError(
                f"CandidateGenerator worker requires worker_role "
                f"'{CANDIDATE_GENERATOR_ROLE}', received '{worker_role}'."
            )

        if "allowed_outputs" in payload and "forbidden_outputs" in payload:
            return json.loads(json.dumps(payload))

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
            raise CandidateGeneratorInputError(
                f"CandidateGenerator slice is missing required fields: {missing}"
            )

        return {
            "schema_version": "1.0.0",
            "slice_id": payload["slice_id"],
            "worker_role": CANDIDATE_GENERATOR_ROLE,
            "task": json.loads(json.dumps(payload["task"])),
            "snapshot_ref": payload["snapshot_ref"],
            "target_paths": json.loads(json.dumps(payload["target_paths"])),
            "relevant_observations": json.loads(json.dumps(payload["relevant_observations"])),
            "open_questions": json.loads(json.dumps(payload["open_questions"])),
            "constraints": dict(CANDIDATE_GENERATOR_CONSTRAINTS),
            "allowed_outputs": json.loads(json.dumps(CANDIDATE_GENERATOR_ALLOWED_OUTPUTS)),
            "forbidden_outputs": json.loads(json.dumps(CANDIDATE_GENERATOR_FORBIDDEN_OUTPUTS)),
        }

    def _validate_worker_input(self, worker_input: dict[str, Any]) -> None:
        errors = sorted(
            self._input_validator.iter_errors(worker_input),
            key=lambda error: self._format_error_path(error),
        )
        if errors:
            formatted = "; ".join(self._format_validation_error(error) for error in errors)
            raise CandidateGeneratorInputError(
                f"CandidateGenerator worker input is invalid: {formatted}"
            )

    def _validate_worker_output(self, payload: dict[str, Any]) -> None:
        errors = sorted(
            self._output_validator.iter_errors(payload),
            key=lambda error: self._format_error_path(error),
        )
        if errors:
            formatted = "; ".join(self._format_validation_error(error) for error in errors)
            raise CandidateGeneratorOutputError(
                f"CandidateGenerator worker output is invalid: {formatted}"
            )

    @staticmethod
    def _validate_output_binding(payload: dict[str, Any], worker_input: dict[str, Any]) -> None:
        if payload["worker_role"] != worker_input["worker_role"]:
            raise CandidateGeneratorOutputError(
                f"CandidateGenerator output worker_role '{payload['worker_role']}' does not match "
                f"input worker_role '{worker_input['worker_role']}'."
            )
        if payload["slice_id"] != worker_input["slice_id"]:
            raise CandidateGeneratorOutputError(
                f"CandidateGenerator output slice_id '{payload['slice_id']}' does not match "
                f"input slice_id '{worker_input['slice_id']}'."
            )
        expected_task_id = worker_input["task"]["id"]
        if payload["task_id"] != expected_task_id:
            raise CandidateGeneratorOutputError(
                f"CandidateGenerator output task_id '{payload['task_id']}' does not match "
                f"input task_id '{expected_task_id}'."
            )

    @staticmethod
    def _validate_candidate_events_only(payload: dict[str, Any]) -> None:
        """Validate that only candidate.proposed events are present."""
        for event in payload.get("candidate_events", []):
            event_type = event.get("event_type")
            if event_type != "candidate.proposed":
                raise CandidateGeneratorOutputError(
                    f"CandidateGenerator may only emit 'candidate.proposed' events, "
                    f"received '{event_type}'."
                )

            entity_type = event.get("entity_type")
            if entity_type != "candidate":
                raise CandidateGeneratorOutputError(
                    f"CandidateGenerator may only emit 'candidate' entity type, "
                    f"received '{entity_type}'."
                )

            candidate_type = event.get("payload", {}).get("candidate_type")
            if candidate_type not in CANDIDATE_TYPES:
                raise CandidateGeneratorOutputError(
                    f"CandidateGenerator candidate_type must be one of "
                    f"{CANDIDATE_TYPES}, received '{candidate_type}'."
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
