from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource


VERIFIER_ROLE = "Verifier"
VERIFIER_ALLOWED_OUTPUTS = {
    "candidate_event_types": [
        "contradiction.registered",
        "hypothesis.rejected",
        "hypothesis.sent_to_verification",
        "hypothesis.supported",
        "hypothesis.unresolved_conflict",
        "observation.rejected",
        "observation.verified",
        "question.opened",
    ]
}
VERIFIER_FORBIDDEN_OUTPUTS = {
    "candidate_event_types": [
        "hypothesis.proposed",
        "issue.proposed",
        "observation.proposed",
    ],
    "actions": [
        "issue_creation",
        "unsupported_claims",
        "inference_without_evidence",
    ],
}
VERIFIER_CONSTRAINTS = {
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


class VerifierWorkerError(Exception):
    """Base error for Verifier worker failures."""


class VerifierInputError(VerifierWorkerError):
    """Raised when Verifier input cannot be loaded or validated."""


class VerifierOutputError(VerifierWorkerError):
    """Raised when Verifier output cannot be parsed or validated."""


@dataclass(frozen=True)
class PreparedVerifierRequest:
    worker_input: dict[str, Any]
    prompt: str


@dataclass(frozen=True)
class VerifierResult:
    payload: dict[str, Any]
    candidate_events: list[dict[str, Any]]


class VerifierWorker:
    """Prepare Verifier worker input and validate structured Verifier output."""

    def __init__(
        self,
        root_dir: str | Path,
        prompt_path: str | Path = "prompts/verifier.md",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.prompt_path = (self.root_dir / prompt_path).resolve()
        self.schema_dir = (self.root_dir / "schema").resolve()
        self._input_validator, self._output_validator = self._build_validators()

    def load_worker_input(self, source: str | Path | dict[str, Any]) -> dict[str, Any]:
        raw_payload = self._load_json_object(
            source,
            error_cls=VerifierInputError,
            label="Verifier input",
        )
        worker_input = self._coerce_to_verifier_contract(raw_payload)
        self._validate_worker_input(worker_input)
        self._validate_target_observation(worker_input)
        return worker_input

    def prepare_request(self, source: str | Path | dict[str, Any]) -> PreparedVerifierRequest:
        worker_input = self.load_worker_input(source)
        prompt = self.render_prompt(worker_input)
        return PreparedVerifierRequest(worker_input=worker_input, prompt=prompt)

    def render_prompt(self, worker_input: dict[str, Any]) -> str:
        if not self.prompt_path.exists():
            raise VerifierWorkerError(f"Verifier prompt file does not exist: {self.prompt_path}")

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
    ) -> VerifierResult:
        expected_input = self.load_worker_input(worker_input) if worker_input is not None else None
        payload = self._load_json_object(
            source,
            error_cls=VerifierOutputError,
            label="Verifier output",
        )
        self._validate_worker_output(payload)

        if expected_input is not None:
            self._validate_output_binding(payload, expected_input)
            self._validate_observation_event_targets(payload, expected_input)
            self._validate_hypothesis_event_targets(payload, expected_input)
            self._validate_hypothesis_verification_basis(payload, expected_input)

        candidate_events = self._normalize_candidate_events(payload["candidate_events"])
        normalized_payload = {
            "schema_version": payload["schema_version"],
            "slice_id": payload["slice_id"],
            "worker_role": payload["worker_role"],
            "task_id": payload["task_id"],
            "candidate_events": candidate_events,
        }
        return VerifierResult(payload=normalized_payload, candidate_events=candidate_events)

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

    def _coerce_to_verifier_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_role = payload.get("worker_role")
        if worker_role != VERIFIER_ROLE:
            raise VerifierInputError(
                f"Verifier worker requires worker_role '{VERIFIER_ROLE}', received '{worker_role}'."
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
            raise VerifierInputError(f"Verifier slice is missing required fields: {missing}")

        return {
            "schema_version": "1.0.0",
            "slice_id": payload["slice_id"],
            "worker_role": VERIFIER_ROLE,
            "task": json.loads(json.dumps(payload["task"])),
            "snapshot_ref": payload["snapshot_ref"],
            "target_paths": json.loads(json.dumps(payload["target_paths"])),
            "relevant_observations": json.loads(json.dumps(payload["relevant_observations"])),
            "open_questions": json.loads(json.dumps(payload["open_questions"])),
            "constraints": dict(VERIFIER_CONSTRAINTS),
            "allowed_outputs": json.loads(json.dumps(VERIFIER_ALLOWED_OUTPUTS)),
            "forbidden_outputs": json.loads(json.dumps(VERIFIER_FORBIDDEN_OUTPUTS)),
        }

    def _validate_worker_input(self, worker_input: dict[str, Any]) -> None:
        errors = sorted(
            self._input_validator.iter_errors(worker_input),
            key=lambda error: self._format_error_path(error),
        )
        if errors:
            formatted = "; ".join(self._format_validation_error(error) for error in errors)
            raise VerifierInputError(f"Verifier worker input is invalid: {formatted}")

    @staticmethod
    def _validate_target_observation(worker_input: dict[str, Any]) -> None:
        target = worker_input["task"]["target"]
        target_observation_id = target["value"]
        relevant_observations = worker_input["relevant_observations"]
        observation = relevant_observations.get(target_observation_id)
        if observation is None:
            raise VerifierInputError(
                f"Verifier slice must include the target observation '{target_observation_id}'."
            )
        if observation.get("id") != target_observation_id:
            raise VerifierInputError(
                f"Target observation entry '{target_observation_id}' must contain matching payload id."
            )

    def _validate_worker_output(self, payload: dict[str, Any]) -> None:
        errors = sorted(
            self._output_validator.iter_errors(payload),
            key=lambda error: self._format_error_path(error),
        )
        if errors:
            formatted = "; ".join(self._format_validation_error(error) for error in errors)
            raise VerifierOutputError(f"Verifier worker output is invalid: {formatted}")

    @staticmethod
    def _validate_output_binding(payload: dict[str, Any], worker_input: dict[str, Any]) -> None:
        if payload["worker_role"] != worker_input["worker_role"]:
            raise VerifierOutputError(
                f"Verifier output worker_role '{payload['worker_role']}' does not match "
                f"input worker_role '{worker_input['worker_role']}'."
            )
        if payload["slice_id"] != worker_input["slice_id"]:
            raise VerifierOutputError(
                f"Verifier output slice_id '{payload['slice_id']}' does not match "
                f"input slice_id '{worker_input['slice_id']}'."
            )
        expected_task_id = worker_input["task"]["id"]
        if payload["task_id"] != expected_task_id:
            raise VerifierOutputError(
                f"Verifier output task_id '{payload['task_id']}' does not match "
                f"input task_id '{expected_task_id}'."
            )

    @staticmethod
    def _validate_observation_event_targets(
        payload: dict[str, Any],
        worker_input: dict[str, Any],
    ) -> None:
        target_observation_id = worker_input["task"]["target"]["value"]
        for event in payload["candidate_events"]:
            if event["event_type"] not in {"observation.verified", "observation.rejected"}:
                continue
            if event["entity_id"] != target_observation_id:
                raise VerifierOutputError(
                    f"Verifier may verify or reject only the target observation "
                    f"'{target_observation_id}', received '{event['entity_id']}'."
                )

    @staticmethod
    def _validate_hypothesis_event_targets(
        payload: dict[str, Any],
        worker_input: dict[str, Any],
    ) -> None:
        """Validate that hypothesis events only target the hypothesis being verified.

        v1.3 Step 2: Hypothesis verification events must target only the hypothesis
        that is the subject of the current verify_claim task.
        """
        target_kind = worker_input["task"]["target"].get("kind")
        if target_kind != "hypothesis":
            return  # Only validate when target is a hypothesis

        target_hypothesis_id = worker_input["task"]["target"]["value"]
        hypothesis_event_types = {
            "hypothesis.sent_to_verification",
            "hypothesis.supported",
            "hypothesis.rejected",
        }
        for event in payload["candidate_events"]:
            if event["event_type"] not in hypothesis_event_types:
                continue
            if event["entity_id"] != target_hypothesis_id:
                raise VerifierOutputError(
                    f"Verifier may only emit hypothesis events for the target hypothesis "
                    f"'{target_hypothesis_id}', received '{event['entity_id']}' for "
                    f"event type '{event['event_type']}'."
                )

    @staticmethod
    def _validate_hypothesis_verification_basis(
        payload: dict[str, Any],
        worker_input: dict[str, Any],
    ) -> None:
        """Validate verification_basis for hypothesis verification events.

        v1.3 Step 4: Enforces structured evidence composition for hypothesis verification.
        - hypothesis.supported MUST include verification_basis
        - verification_basis.supporting_observations MUST be non-empty for supported status
        - verification_basis fields must be properly typed
        """
        target_kind = worker_input["task"]["target"].get("kind")
        if target_kind != "hypothesis":
            return  # Only validate when target is a hypothesis

        available_observations = set(worker_input.get("relevant_observations", {}).keys())
        available_hypotheses = set(worker_input.get("relevant_hypotheses", {}).keys())
        relevant_hypotheses = worker_input.get("relevant_hypotheses", {})

        for event in payload["candidate_events"]:
            event_type = event.get("event_type")

            if event_type == "hypothesis.supported":
                VerifierWorker._validate_supported_hypothesis_basis(
                    event, available_observations, available_hypotheses
                )
            elif event_type == "hypothesis.rejected":
                VerifierWorker._validate_rejected_hypothesis_basis(
                    event, available_observations, available_hypotheses
                )
            elif event_type == "hypothesis.unresolved_conflict":
                VerifierWorker._validate_unresolved_conflict_hypothesis_basis(
                    event, available_observations, available_hypotheses, relevant_hypotheses
                )

    @staticmethod
    def _validate_supported_hypothesis_basis(
        event: dict[str, Any],
        available_observations: set[str],
        available_hypotheses: set[str],
    ) -> None:
        """Validate verification_basis for hypothesis.supported events."""
        payload = event.get("payload", {})
        event_id = event.get("entity_id", "unknown")

        verification_basis = payload.get("verification_basis")
        if not isinstance(verification_basis, dict):
            raise VerifierOutputError(
                f"hypothesis.supported event for '{event_id}' must include "
                f"verification_basis object."
            )

        # Validate supporting_observations (REQUIRED for supported, must be non-empty)
        supporting_observations = verification_basis.get("supporting_observations")
        if not isinstance(supporting_observations, list):
            raise VerifierOutputError(
                f"hypothesis.supported event for '{event_id}' must have "
                f"verification_basis.supporting_observations as array."
            )
        if len(supporting_observations) == 0:
            raise VerifierOutputError(
                f"hypothesis.supported event for '{event_id}' requires at least one "
                f"supporting_observation (no single-fact shortcut)."
            )

        # Validate observation IDs reference available observations
        for obs_id in supporting_observations:
            if not isinstance(obs_id, str):
                raise VerifierOutputError(
                    f"supporting_observations must contain string IDs, "
                    f"found {type(obs_id).__name__} in event for '{event_id}'."
                )
            if obs_id not in available_observations:
                raise VerifierOutputError(
                    f"supporting_observations contains '{obs_id}' which is not in "
                    f"the provided evidence set for event '{event_id}'."
                )

        # Validate supporting_hypotheses if present
        supporting_hyps = verification_basis.get("supporting_hypotheses")
        if supporting_hyps is not None:
            if not isinstance(supporting_hyps, list):
                raise VerifierOutputError(
                    f"verification_basis.supporting_hypotheses must be array "
                    f"in event for '{event_id}'."
                )
            for hyp_id in supporting_hyps:
                if not isinstance(hyp_id, str):
                    raise VerifierOutputError(
                        f"supporting_hypotheses must contain string IDs "
                        f"in event for '{event_id}'."
                    )
                if hyp_id not in available_hypotheses:
                    raise VerifierOutputError(
                        f"supporting_hypotheses contains '{hyp_id}' which is not in "
                        f"the provided hypothesis context for event '{event_id}'."
                    )

        # Validate missing_evidence if present
        missing_evidence = verification_basis.get("missing_evidence")
        if missing_evidence is not None:
            if not isinstance(missing_evidence, list):
                raise VerifierOutputError(
                    f"verification_basis.missing_evidence must be array "
                    f"in event for '{event_id}'."
                )

        # Validate contradictions_detected if present
        contradictions = verification_basis.get("contradictions_detected")
        if contradictions is not None:
            if not isinstance(contradictions, list):
                raise VerifierOutputError(
                    f"verification_basis.contradictions_detected must be array "
                    f"in event for '{event_id}'."
                )
            for c in contradictions:
                if not isinstance(c, dict):
                    raise VerifierOutputError(
                        f"contradictions_detected entries must be objects "
                        f"in event for '{event_id}'."
                    )
                if "contradicting_hypothesis_id" not in c:
                    raise VerifierOutputError(
                        f"contradictions_detected entries must have "
                        f"contradicting_hypothesis_id in event for '{event_id}'."
                    )
                if not isinstance(c.get("description"), str) or not c.get("description"):
                    raise VerifierOutputError(
                        f"contradictions_detected entries must have non-empty description "
                        f"in event for '{event_id}'."
                    )

    @staticmethod
    def _validate_rejected_hypothesis_basis(
        event: dict[str, Any],
        available_observations: set[str],
        available_hypotheses: set[str],
    ) -> None:
        """Validate verification_basis for hypothesis.rejected events.

        Rejected hypotheses may have empty supporting_observations (evidence contradicts),
        but if verification_basis is present, validate its structure.
        """
        payload = event.get("payload", {})
        event_id = event.get("entity_id", "unknown")

        verification_basis = payload.get("verification_basis")
        if verification_basis is None:
            return  # verification_basis is optional for rejected

        if not isinstance(verification_basis, dict):
            raise VerifierOutputError(
                f"hypothesis.rejected event for '{event_id}' has invalid "
                f"verification_basis (must be object or absent)."
            )

        # Validate supporting_observations if present
        supporting_observations = verification_basis.get("supporting_observations")
        if supporting_observations is not None:
            if not isinstance(supporting_observations, list):
                raise VerifierOutputError(
                    f"verification_basis.supporting_observations must be array "
                    f"in event for '{event_id}'."
                )

        # Validate contradictions_detected if present
        contradictions = verification_basis.get("contradictions_detected")
        if contradictions is not None:
            if not isinstance(contradictions, list):
                raise VerifierOutputError(
                    f"verification_basis.contradictions_detected must be array "
                    f"in event for '{event_id}'."
                )

    @staticmethod
    def _validate_unresolved_conflict_hypothesis_basis(
        event: dict[str, Any],
        available_observations: set[str],
        available_hypotheses: set[str],
        relevant_hypotheses: dict[str, dict[str, Any]],
    ) -> None:
        """Validate verification_basis for hypothesis.unresolved_conflict events.

        v1.3 Step 5 HARDENED: Unresolved conflict indicates competing hypotheses with
        evidence on multiple sides. This is NOT an escape hatch - strict requirements
        prevent misuse.

        Requirements:
        - MUST include verification_basis object
        - MUST include conflict_context describing the competing evidence
        - MUST include missing_evidence (non-empty) explaining what resolves the conflict
        - MUST reference at least two competing hypotheses in conflict_context
        - EACH competing hypothesis entry MUST have non-empty supporting_observations
        - ALL hypothesis_ids MUST exist in available_hypotheses
        - ALL observation_ids in entries MUST exist in available_observations
        - At least ONE pair of competing hypotheses must have a contradiction relationship
        """
        payload = event.get("payload", {})
        event_id = event.get("entity_id", "unknown")

        verification_basis = payload.get("verification_basis")
        if not isinstance(verification_basis, dict):
            raise VerifierOutputError(
                f"hypothesis.unresolved_conflict event for '{event_id}' must include "
                f"verification_basis object."
            )

        # Validate conflict_context (REQUIRED for unresolved_conflict)
        conflict_context = verification_basis.get("conflict_context")
        if not isinstance(conflict_context, dict):
            raise VerifierOutputError(
                f"hypothesis.unresolved_conflict event for '{event_id}' must have "
                f"verification_basis.conflict_context as object."
            )

        # Validate competing_hypotheses (REQUIRED, must have at least 2)
        competing_hypotheses = conflict_context.get("competing_hypotheses")
        if not isinstance(competing_hypotheses, list):
            raise VerifierOutputError(
                f"conflict_context.competing_hypotheses must be array "
                f"in event for '{event_id}'."
            )
        if len(competing_hypotheses) < 2:
            raise VerifierOutputError(
                f"conflict_context.competing_hypotheses must have at least 2 "
                f"competing hypotheses in event for '{event_id}'."
            )

        # Track all hypothesis IDs for contradiction relationship checking
        all_competing_ids: set[str] = set()
        entries_with_evidence = 0

        # Validate each competing hypothesis entry
        for idx, hyp_ref in enumerate(competing_hypotheses):
            if not isinstance(hyp_ref, dict):
                raise VerifierOutputError(
                    f"competing_hypotheses[{idx}] must be object "
                    f"in event for '{event_id}'."
                )

            # Validate hypothesis_id (REQUIRED, must exist in available_hypotheses)
            hyp_id = hyp_ref.get("hypothesis_id")
            if not isinstance(hyp_id, str):
                raise VerifierOutputError(
                    f"competing_hypotheses[{idx}].hypothesis_id must be string "
                    f"in event for '{event_id}'."
                )
            if hyp_id not in available_hypotheses:
                raise VerifierOutputError(
                    f"competing_hypotheses[{idx}].hypothesis_id '{hyp_id}' is not in "
                    f"the provided hypothesis context for event '{event_id}'. "
                    f"Fabricated hypothesis IDs are not allowed."
                )
            all_competing_ids.add(hyp_id)

            # STRICT: Validate supporting_observations (REQUIRED for each entry, must be non-empty)
            entry_supporting_obs = hyp_ref.get("supporting_observations")
            if not isinstance(entry_supporting_obs, list):
                raise VerifierOutputError(
                    f"competing_hypotheses[{idx}].supporting_observations must be array "
                    f"in event for '{event_id}'."
                )
            if len(entry_supporting_obs) == 0:
                raise VerifierOutputError(
                    f"competing_hypotheses[{idx}].supporting_observations must be non-empty "
                    f"for hypothesis '{hyp_id}' in event for '{event_id}'. "
                    f"Unresolved conflict requires evidence on BOTH sides - "
                    f"empty evidence means this side has no support and should be rejected."
                )

            # Validate all observation IDs exist in available_observations
            for obs_id in entry_supporting_obs:
                if not isinstance(obs_id, str):
                    raise VerifierOutputError(
                        f"competing_hypotheses[{idx}].supporting_observations must contain "
                        f"string IDs in event for '{event_id}'."
                    )
                if obs_id not in available_observations:
                    raise VerifierOutputError(
                        f"competing_hypotheses[{idx}].supporting_observations contains "
                        f"'{obs_id}' which is not in the provided evidence set for "
                        f"event '{event_id}'. Fabricated observation IDs are not allowed."
                    )

            entries_with_evidence += 1

            # Validate summary (REQUIRED)
            summary = hyp_ref.get("summary")
            if not isinstance(summary, str) or not summary:
                raise VerifierOutputError(
                    f"competing_hypotheses[{idx}].summary must be non-empty string "
                    f"in event for '{event_id}'."
                )

        # STRICT: Require at least 2 entries with evidence (bidirectional evidence requirement)
        if entries_with_evidence < 2:
            raise VerifierOutputError(
                f"At least 2 competing hypotheses must have supporting_observations "
                f"in event for '{event_id}'. Unresolved conflict requires evidence on BOTH sides."
            )

        # Validate conflict_description (REQUIRED)
        conflict_description = conflict_context.get("conflict_description")
        if not isinstance(conflict_description, str) or not conflict_description:
            raise VerifierOutputError(
                f"conflict_context.conflict_description must be non-empty string "
                f"in event for '{event_id}'."
            )

        # STRICT: Validate missing_evidence (REQUIRED, must be non-empty)
        missing_evidence = verification_basis.get("missing_evidence")
        if not isinstance(missing_evidence, list):
            raise VerifierOutputError(
                f"verification_basis.missing_evidence must be array "
                f"in event for '{event_id}'."
            )
        if len(missing_evidence) == 0:
            raise VerifierOutputError(
                f"verification_basis.missing_evidence must be non-empty "
                f"in event for '{event_id}'. Unresolved conflict MUST specify what "
                f"evidence would resolve the conflict."
            )
        for item in missing_evidence:
            if not isinstance(item, str) or not item:
                raise VerifierOutputError(
                    f"verification_basis.missing_evidence must contain non-empty strings "
                    f"in event for '{event_id}'."
                )

        # Validate top-level supporting_observations if present
        supporting_observations = verification_basis.get("supporting_observations")
        if supporting_observations is not None:
            if not isinstance(supporting_observations, list):
                raise VerifierOutputError(
                    f"verification_basis.supporting_observations must be array "
                    f"in event for '{event_id}'."
                )
            for obs_id in supporting_observations:
                if not isinstance(obs_id, str):
                    raise VerifierOutputError(
                        f"supporting_observations must contain string IDs "
                        f"in event for '{event_id}'."
                    )
                if obs_id not in available_observations:
                    raise VerifierOutputError(
                        f"supporting_observations contains '{obs_id}' which is not in "
                        f"the provided evidence set for event '{event_id}'."
                    )

        # STRICT: Validate contradiction relationship exists between at least one pair
        has_contradiction_relationship = False
        competing_ids_list = list(all_competing_ids)

        for i, hyp_id_a in enumerate(competing_ids_list):
            hyp_a_data = relevant_hypotheses.get(hyp_id_a, {})
            contradicting_ids_a = set(hyp_a_data.get("contradicting_hypothesis_ids", []))

            for hyp_id_b in competing_ids_list[i + 1:]:
                # Check if A contradicts B or B contradicts A
                if hyp_id_b in contradicting_ids_a:
                    has_contradiction_relationship = True
                    break

                hyp_b_data = relevant_hypotheses.get(hyp_id_b, {})
                contradicting_ids_b = set(hyp_b_data.get("contradicting_hypothesis_ids", []))
                if hyp_id_a in contradicting_ids_b:
                    has_contradiction_relationship = True
                    break

            if has_contradiction_relationship:
                break

        if not has_contradiction_relationship:
            raise VerifierOutputError(
                f"No contradiction relationship found between competing hypotheses "
                f"in event for '{event_id}'. Hypotheses {competing_ids_list} do not have "
                f"contradicting_hypothesis_ids pointing to each other. "
                f"Unresolved conflict requires actual contradiction, not just different hypotheses."
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
