from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from runtime.adapters.base import (
    BackendCapabilities,
    BackendFailure,
    BackendFailureKind,
    BackendInvocationResult,
    BackendPolicyEnvelope,
    BackendTelemetry,
    OutcomeLevel,
    classify_exception,
)
from runtime.workers.issue_composer import (
    IssueComposerResult,
    IssueComposerWorker,
    IssueComposerWorkerError,
)
from runtime.evidence import derive_observation_evidence_class, normalize_evidence_class
from runtime.workers.candidate_generator import (
    CandidateGeneratorResult,
    CandidateGeneratorWorker,
    CandidateGeneratorWorkerError,
)
from runtime.workers.reader import ReaderResult, ReaderWorker, ReaderWorkerError
from runtime.workers.verifier import VerifierResult, VerifierWorker, VerifierWorkerError


WorkerInputSource = str | Path | dict[str, Any]
WorkerResult = ReaderResult | VerifierResult | IssueComposerResult | CandidateGeneratorResult
CodexExecutor = Callable[["CodexInvocationRequest"], str]
CODEX_EXECUTABLE_ENV = "CODEX_EXECUTABLE"
TRANSPORT_PAYLOAD_JSON_KEY = "payload_json"
TRANSPORT_PAYLOAD_KEY = "payload"
INTERNAL_EVENT_REQUIRED_KEYS = {
    "schema_version",
    "id",
    "audit_id",
    "entity_type",
    "entity_id",
    "event_type",
    "occurred_at",
    "actor",
    "snapshot_ref",
    "idempotency_key",
    "caused_by_event_id",
    "payload",
    "acceptance",
}


class CodexAdapterError(Exception):
    """Base error for Codex adapter failures."""


class UnsupportedWorkerRoleError(CodexAdapterError):
    """Raised when a worker role is not supported by the adapter."""


class CodexInvocationError(CodexAdapterError):
    """Raised when the Codex process cannot be invoked successfully."""


class CodexOutputError(CodexAdapterError):
    """Raised when Codex returns invalid or non-structured output."""


@dataclass(frozen=True)
class CodexInvocationRequest:
    command: tuple[str, ...]
    prompt: str
    invocation_dir: str
    subprocess_cwd: str
    output_path: str
    output_schema_path: str


@dataclass(frozen=True)
class CodexRunResult:
    worker_role: str
    worker_input: dict[str, Any]
    prompt: str
    raw_output: str
    normalized_output: dict[str, Any]
    candidate_events: list[dict[str, Any]]
    input_digest: str
    output_digest: str
    prompt_digest: str
    raw_output_digest: str
    invocation_metadata: dict[str, Any]


class CodexAdapter:
    """Stateless adapter that runs one worker call through Codex CLI."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        invocation_dir: str | Path | None = None,
        codex_command: tuple[str, ...] | list[str] | None = None,
        model: str | None = None,
        sandbox_mode: str = "read-only",
        timeout_seconds: int = 300,
        executor: CodexExecutor | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.invocation_dir = (
            Path(invocation_dir).resolve() if invocation_dir is not None else self.root_dir
        )
        self.output_schema_path = (
            self.root_dir / "schema" / "codex_transport_output.schema.json"
        ).resolve()
        self.codex_command_override = self._normalize_codex_command_override(codex_command)
        self.model = model
        self.sandbox_mode = sandbox_mode
        self.timeout_seconds = timeout_seconds
        self.executor = executor or self._invoke_codex
        self._workers = {
            "Reader": ReaderWorker(self.root_dir),
            "Verifier": VerifierWorker(self.root_dir),
            "IssueComposer": IssueComposerWorker(self.root_dir),
            "CandidateGenerator": CandidateGeneratorWorker(self.root_dir),
        }

    def run(self, worker_role: str, worker_input: WorkerInputSource) -> list[dict[str, Any]]:
        return self.run_with_result(worker_role, worker_input).candidate_events

    def run_with_result(
        self,
        worker_role: str,
        worker_input: WorkerInputSource,
    ) -> CodexRunResult:
        failure_stage = "resolve_worker"
        prepared_request = None
        transport_prompt: str | None = None
        invocation: CodexInvocationRequest | None = None
        raw_output: str | None = None
        normalized_output: dict[str, Any] | None = None
        try:
            worker = self._get_worker(worker_role)
            failure_stage = "prepare_request"
            prepared_request = self._prepare_request(worker_role, worker, worker_input)
            failure_stage = "assemble_prompt"
            transport_prompt = self._wrap_prompt_for_transport(prepared_request.prompt)
            failure_stage = "build_invocation"
            invocation = self._build_invocation(transport_prompt)
            failure_stage = "invoke_codex"
            raw_output = self.executor(invocation)
            failure_stage = "validate_worker_result"
            if not isinstance(raw_output, str):
                raise CodexInvocationError("Codex executor must return the raw worker output as a string.")
            if not raw_output.strip():
                raise CodexOutputError(f"Codex returned empty output for worker role '{worker_role}'.")

            failure_stage = "parse_worker_output"
            parsed_result = self._parse_output(
                worker_role,
                worker,
                raw_output,
                prepared_request.worker_input,
            )
            invocation_metadata = self._build_invocation_metadata(invocation)
            worker_input_copy = json.loads(json.dumps(prepared_request.worker_input))
            normalized_output = json.loads(json.dumps(parsed_result.payload))
        except CodexAdapterError as exc:
            raise self._augment_failure(
                exc,
                worker_role=worker_role,
                worker_input=prepared_request.worker_input if prepared_request is not None else None,
                transport_prompt=transport_prompt,
                invocation=invocation,
                raw_output=raw_output,
                normalized_output=normalized_output,
                failure_stage=failure_stage,
            ) from exc
        except Exception as exc:
            wrapped = CodexInvocationError(
                f"Codex invocation failed for worker role '{worker_role}': {exc}"
            )
            raise self._augment_failure(
                wrapped,
                worker_role=worker_role,
                worker_input=prepared_request.worker_input if prepared_request is not None else None,
                transport_prompt=transport_prompt,
                invocation=invocation,
                raw_output=raw_output,
                normalized_output=normalized_output,
                failure_stage=failure_stage,
            ) from exc
        finally:
            if invocation is not None:
                self._cleanup_output_path(invocation.output_path)

        return CodexRunResult(
            worker_role=worker_role,
            worker_input=worker_input_copy,
            prompt=transport_prompt,
            raw_output=raw_output,
            normalized_output=normalized_output,
            candidate_events=json.loads(json.dumps(parsed_result.candidate_events)),
            input_digest=self._sha256_json(worker_input_copy),
            output_digest=self._sha256_json(normalized_output),
            prompt_digest=self._sha256_text(transport_prompt),
            raw_output_digest=self._sha256_text(raw_output),
            invocation_metadata=invocation_metadata,
        )

    def _get_worker(self, worker_role: str) -> ReaderWorker | VerifierWorker | IssueComposerWorker | CandidateGeneratorWorker:
        worker = self._workers.get(worker_role)
        if worker is None:
            supported_roles = ", ".join(sorted(self._workers))
            raise UnsupportedWorkerRoleError(
                f"Unsupported worker role '{worker_role}'. Supported roles: {supported_roles}."
            )
        return worker

    @staticmethod
    def _prepare_request(
        worker_role: str,
        worker: ReaderWorker | VerifierWorker | IssueComposerWorker | CandidateGeneratorWorker,
        worker_input: WorkerInputSource,
    ) -> Any:
        try:
            return worker.prepare_request(worker_input)
        except (ReaderWorkerError, VerifierWorkerError, IssueComposerWorkerError, CandidateGeneratorWorkerError) as exc:
            raise CodexAdapterError(
                f"Worker input is invalid for role '{worker_role}': {exc}"
            ) from exc

    def _augment_failure(
        self,
        exc: CodexAdapterError,
        *,
        worker_role: str,
        worker_input: dict[str, Any] | None,
        transport_prompt: str | None,
        invocation: CodexInvocationRequest | None,
        raw_output: str | None,
        normalized_output: dict[str, Any] | None,
        failure_stage: str,
    ) -> CodexAdapterError:
        exc.worker_role = worker_role
        exc.failure_stage = getattr(exc, "failure_stage", None) or failure_stage
        exc.input_digest = (
            self._sha256_json(worker_input) if isinstance(worker_input, dict) else None
        )
        exc.output_digest = (
            self._sha256_json(normalized_output) if isinstance(normalized_output, dict) else None
        )
        exc.prompt_digest = (
            self._sha256_text(transport_prompt) if isinstance(transport_prompt, str) else None
        )
        exc.raw_output_digest = (
            self._sha256_text(raw_output) if isinstance(raw_output, str) and raw_output else None
        )
        exc.invocation_metadata = (
            self._build_invocation_metadata(invocation)
            if invocation is not None
            else self._build_base_invocation_metadata()
        )
        return exc

    @staticmethod
    def _normalize_codex_command_override(
        codex_command: tuple[str, ...] | list[str] | None,
    ) -> tuple[str, ...] | None:
        if codex_command is not None:
            normalized = tuple(str(part) for part in codex_command if str(part))
            if not normalized:
                raise CodexInvocationError("codex_command override must contain at least one argument.")
            return normalized
        return None

    def _resolve_codex_command(self) -> tuple[str, ...]:
        codex_command = self.codex_command_override
        if codex_command is not None:
            return codex_command

        env_override = os.environ.get(CODEX_EXECUTABLE_ENV, "").strip()
        if env_override:
            return (env_override,)

        resolved = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.CMD")
        if resolved is not None:
            return (resolved,)

        raise CodexInvocationError(
            "Codex executable could not be resolved. Pass codex_command=... or set "
            f"{CODEX_EXECUTABLE_ENV}."
        )

    @staticmethod
    def _wrap_prompt_for_transport(prompt: str) -> str:
        transport_instructions = """

Transport Output Envelope:
- Your final top-level JSON object must contain exactly one key: `payload_json`.
- `payload_json` must be a JSON string whose decoded value is the transport worker output object described below.
- The decoded `payload_json` value must be a JSON object, not prose and not markdown.
- Do not rename `payload_json`.
- Do not add any sibling keys next to `payload_json`.

Transport Worker Output Shape:
{
  "payload_json": "{\"schema_version\":\"1.0.0\",\"slice_id\":\"<copy from input>\",\"worker_role\":\"<role>\",\"task_id\":\"<copy from input.task.id>\",\"candidate_events\":[{\"event_type\":\"observation.proposed\",\"payload\":{\"claim\":\"<claim text>\",\"evidence\":[{\"file_path\":\"src/app.py\",\"line_start\":3,\"line_end\":4,\"snapshot_ref\":\"<snapshot_ref>\"}]}}]}"
}

Transport Candidate Event Rules:
- Keep `schema_version`, `slice_id`, `worker_role`, and `task_id` at the transport worker-output level.
- For each entry in `candidate_events`, emit only:
  - `event_type`
  - `payload`
- Do not emit internal event-envelope fields such as:
  - `id`
  - `entity_id`
  - `entity_type`
  - `occurred_at`
  - `actor`
  - `idempotency_key`
  - `caused_by_event_id`
  - `acceptance`
  - `pending`
- The adapter will deterministically add internal metadata after transport parsing.

Transport Payload Rules:
- For `observation.proposed`, `hypothesis.proposed`, `observation.verified`, and `observation.rejected`:
  - put the main text in `payload.claim`
  - put source-bound evidence in `payload.evidence`
- For `question.opened`:
  - put the question text in `payload.question`
  - put any supporting source-bound evidence in `payload.evidence`
- For `issue.proposed`:
  - use `payload.title`
  - use `payload.summary`
  - use `payload.severity` and `payload.severity_rule_ref` when severity is present
  - use `payload.evidence.observation_ids`, `payload.evidence.question_ids`, and `payload.evidence.contradiction_ids` when applicable
- For `contradiction.registered`:
  - use `payload.summary`
  - use `payload.conflicting_entity_refs`
  - use `payload.source_refs`

Transport Evidence Item Shape:
- Each entry in `payload.evidence` may include:
  - `file_path`
  - `line_start`
  - `line_end`
  - `snapshot_ref`
  - optional `file_hash`
  - optional `excerpt`
""".rstrip()
        return prompt.rstrip() + transport_instructions + "\n"

    def _build_invocation(self, prompt: str) -> CodexInvocationRequest:
        if not self.output_schema_path.exists():
            raise CodexAdapterError(
                f"Codex transport schema file does not exist: {self.output_schema_path}"
            )

        output_handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="codex_adapter_",
            suffix=".json",
            delete=False,
        )
        output_handle.close()
        output_path = Path(output_handle.name).resolve()
        try:
            command = list(self._resolve_codex_command())
        except CodexInvocationError as exc:
            exc.failure_stage = "resolve_executable"
            raise
        command.extend(
            [
                "exec",
                "--cd",
                str(self.invocation_dir),
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "--output-schema",
                str(self.output_schema_path),
                "--output-last-message",
                str(output_path),
                "--sandbox",
                self.sandbox_mode,
            ]
        )
        if self.model is not None:
            command.extend(["--model", self.model])
        command.append("-")
        return CodexInvocationRequest(
            command=tuple(command),
            prompt=prompt,
            invocation_dir=str(self.invocation_dir),
            subprocess_cwd=str(self.root_dir),
            output_path=str(output_path),
            output_schema_path=str(self.output_schema_path),
        )

    def _build_invocation_metadata(self, invocation: CodexInvocationRequest) -> dict[str, Any]:
        return {
            "executable": invocation.command[0],
            "subcommand": "exec",
            "invocation_dir": invocation.invocation_dir,
            "subprocess_cwd": invocation.subprocess_cwd,
            "output_schema_path": invocation.output_schema_path,
            "sandbox_mode": self.sandbox_mode,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "skip_git_repo_check": True,
            "ephemeral": True,
            "color": "never",
        }

    def _build_base_invocation_metadata(self) -> dict[str, Any]:
        executable = None
        if self.codex_command_override is not None:
            executable = self.codex_command_override[0]
        else:
            env_override = os.environ.get(CODEX_EXECUTABLE_ENV, "").strip()
            if env_override:
                executable = env_override

        return {
            "executable": executable or "codex",
            "subcommand": "exec",
            "invocation_dir": str(self.invocation_dir),
            "subprocess_cwd": str(self.root_dir),
            "output_schema_path": str(self.output_schema_path),
            "sandbox_mode": self.sandbox_mode,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "skip_git_repo_check": True,
            "ephemeral": True,
            "color": "never",
        }

    def _invoke_codex(self, invocation: CodexInvocationRequest) -> str:
        try:
            completed = subprocess.run(
                invocation.command,
                input=invocation.prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=invocation.subprocess_cwd,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            executable = invocation.command[0] if invocation.command else "codex"
            error = CodexInvocationError(f"Codex executable was not found: {executable}")
            error.failure_stage = "invoke_codex"
            raise error from exc
        except subprocess.TimeoutExpired as exc:
            error = CodexInvocationError(
                f"Codex invocation timed out after {self.timeout_seconds} seconds."
            )
            error.failure_stage = "invoke_codex"
            raise error from exc

        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            if not details:
                details = f"process exited with code {completed.returncode}"
            error = CodexInvocationError(f"Codex invocation failed: {details}")
            error.failure_stage = "invoke_codex"
            raise error

        output_path = Path(invocation.output_path)
        if output_path.exists():
            raw_output = output_path.read_text(encoding="utf-8")
            if raw_output.strip():
                return raw_output

        if completed.stdout.strip():
            return completed.stdout

        error = CodexOutputError("Codex did not produce a final worker message.")
        error.failure_stage = "validate_worker_result"
        raise error

    @staticmethod
    def _parse_output(
        worker_role: str,
        worker: ReaderWorker | VerifierWorker | IssueComposerWorker | CandidateGeneratorWorker,
        raw_output: str,
        worker_input: dict[str, Any],
    ) -> WorkerResult:
        try:
            transport_payload = CodexAdapter._extract_transport_payload(raw_output)
        except CodexOutputError as exc:
            exc.failure_stage = getattr(exc, "failure_stage", None) or "parse_transport_output"
            raise
        try:
            normalized_payload = CodexAdapter._normalize_transport_worker_output(
                worker_role,
                transport_payload,
                worker_input,
            )
        except CodexOutputError as exc:
            exc.failure_stage = getattr(exc, "failure_stage", None) or "normalize_transport_output"
            raise
        try:
            return worker.parse_output(normalized_payload, worker_input=worker_input)
        except (ReaderWorkerError, VerifierWorkerError, IssueComposerWorkerError, CandidateGeneratorWorkerError) as exc:
            error = CodexOutputError(
                f"Codex returned invalid structured output for role '{worker_role}': {exc}"
            )
            error.failure_stage = "validate_worker_output"
            raise error from exc

    @staticmethod
    def _extract_transport_payload(raw_output: str) -> dict[str, Any]:
        try:
            decoded = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise CodexOutputError("Codex transport output must be valid JSON.") from exc

        if not isinstance(decoded, dict):
            raise CodexOutputError("Codex transport output must decode to a JSON object.")

        if TRANSPORT_PAYLOAD_JSON_KEY in decoded:
            if set(decoded.keys()) != {TRANSPORT_PAYLOAD_JSON_KEY}:
                raise CodexOutputError(
                    "Codex transport output must contain only the top-level key 'payload_json'."
                )

            payload_json = decoded[TRANSPORT_PAYLOAD_JSON_KEY]
            if not isinstance(payload_json, str):
                raise CodexOutputError("Codex transport payload_json must be a JSON string.")

            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise CodexOutputError(
                    "Codex transport payload_json must itself contain valid JSON."
                ) from exc

            if not isinstance(payload, dict):
                raise CodexOutputError(
                    "Decoded Codex transport payload_json must produce a JSON object."
                )
            return payload

        if TRANSPORT_PAYLOAD_KEY not in decoded:
            return decoded

        if set(decoded.keys()) != {TRANSPORT_PAYLOAD_KEY}:
            raise CodexOutputError(
                "Codex transport output must contain only the top-level key 'payload'."
            )

        payload = decoded[TRANSPORT_PAYLOAD_KEY]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise CodexOutputError(
                    "Codex transport payload string must itself be valid JSON."
                ) from exc

        if not isinstance(payload, dict):
            raise CodexOutputError("Codex transport payload must decode to a JSON object.")
        return payload

    @staticmethod
    def _normalize_transport_worker_output(
        worker_role: str,
        payload: dict[str, Any],
        worker_input: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_events = payload.get("candidate_events")
        if not isinstance(candidate_events, list):
            return payload

        occurred_at = CodexAdapter._utc_now()
        normalized_events = [
            CodexAdapter._normalize_transport_candidate_event(
                worker_role,
                worker_input,
                candidate_event,
                index=index,
                occurred_at=occurred_at,
            )
            for index, candidate_event in enumerate(candidate_events)
        ]
        return {
            "schema_version": "1.0.0",
            "slice_id": worker_input["slice_id"],
            "worker_role": worker_input["worker_role"],
            "task_id": worker_input["task"]["id"],
            "candidate_events": normalized_events,
        }

    @staticmethod
    def _normalize_transport_candidate_event(
        worker_role: str,
        worker_input: dict[str, Any],
        candidate_event: Any,
        *,
        index: int,
        occurred_at: str,
    ) -> dict[str, Any]:
        if not isinstance(candidate_event, dict):
            raise CodexOutputError("Each transport candidate event must be a JSON object.")

        if INTERNAL_EVENT_REQUIRED_KEYS.issubset(candidate_event):
            result = json.loads(json.dumps(candidate_event))
            # Normalize hypothesis payloads from LLM that may use legacy field values
            if result.get("event_type") == "hypothesis.proposed":
                p = result.get("payload")
                if isinstance(p, dict):
                    if p.get("status") not in (
                        "proposed", "in_verification", "supported", "rejected", "unresolved_conflict",
                    ):
                        p["status"] = "proposed"
                    p.setdefault("confidence", "medium")
                    p.setdefault("origin", "reader_inference")
            return result

        event_type = candidate_event.get("event_type") or candidate_event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise CodexOutputError("Transport candidate events must include a non-empty event_type.")

        payload = candidate_event.get("payload")
        if not isinstance(payload, dict):
            raise CodexOutputError(
                f"Transport candidate event '{event_type}' must include a JSON object payload."
            )

        event_payload, entity_type, entity_id = CodexAdapter._build_internal_entity_payload(
            worker_role,
            worker_input,
            event_type,
            payload,
            index=index,
            occurred_at=occurred_at,
        )
        fingerprint = CodexAdapter._canonical_json(
            {
                "task_id": worker_input["task"]["id"],
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": event_payload,
                "index": index,
            }
        )
        digest = hashlib.sha256(fingerprint.encode("ascii")).hexdigest()[:16]
        return {
            "schema_version": "1.0.0",
            "id": f"event_{digest}",
            "audit_id": worker_input["task"]["audit_id"],
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "actor": {
                "actor_type": "worker",
                "actor_id": f"codex-{worker_role.lower()}",
                "role": worker_role,
            },
            "snapshot_ref": worker_input["snapshot_ref"],
            "idempotency_key": f"{worker_input['task']['id']}:{event_type}:{digest}",
            "caused_by_event_id": None,
            "payload": event_payload,
            "acceptance": {
                "status": "pending",
                "decided_at": None,
                "decided_by": None,
                "reason": None,
            },
        }

    @staticmethod
    def _build_internal_entity_payload(
        worker_role: str,
        worker_input: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        index: int,
        occurred_at: str,
    ) -> tuple[dict[str, Any], str, str]:
        audit_id = worker_input["task"]["audit_id"]
        snapshot_ref = worker_input["snapshot_ref"]
        transport_source_refs = CodexAdapter._extract_transport_source_refs(payload, snapshot_ref)

        if event_type == "observation.proposed":
            claim = CodexAdapter._extract_transport_text(payload, "claim", "statement")
            if not transport_source_refs:
                raise CodexOutputError("Transport observation.proposed requires source-bound evidence.")
            # Detect pattern provenance: stable IDs of overlapping pattern matches
            pattern_matches = worker_input.get("pattern_matches") or []
            overlapping_ids = CodexAdapter._find_overlapping_pattern_ids(
                pattern_matches, transport_source_refs,
            )
            explicit_pm_ids = CodexAdapter._extract_string_list(payload.get("pattern_match_ids"))
            merged_pm_ids = list(dict.fromkeys(explicit_pm_ids + overlapping_ids))

            # Determine evidence_origin from pattern linkage
            if merged_pm_ids:
                evidence_origin = payload.get("evidence_origin") or (
                    "deterministic_pattern" if not explicit_pm_ids else "mixed_pattern_model"
                )
            else:
                evidence_origin = payload.get("evidence_origin") or "model_discovered"

            # evidence_class: explicit > derive from source_refs (no longer auto-set pattern_match)
            enriched_payload = dict(payload)
            if "evidence_class" not in enriched_payload:
                # Let _resolve derive from source_refs only (no pattern_match injection)
                pass
            evidence_class = CodexAdapter._resolve_observation_evidence_class(
                payload=enriched_payload,
                source_refs=transport_source_refs,
            )

            confidence = payload.get("confidence") or (
                "high" if evidence_origin == "deterministic_pattern" else "medium"
            )

            entity_id = CodexAdapter._build_entity_id("obs", worker_input, event_type, payload, index)
            obs_result: dict[str, Any] = {
                "id": entity_id,
                "audit_id": audit_id,
                "status": "proposed",
                "statement": claim,
                "evidence_class": evidence_class,
                "evidence_origin": evidence_origin,
                "provenance": {"source_refs": transport_source_refs},
                "created_at": occurred_at,
                "updated_at": occurred_at,
            }
            if merged_pm_ids:
                obs_result["pattern_match_ids"] = merged_pm_ids
            obs_result["confidence"] = confidence
            return obs_result, "observation", entity_id

        if event_type == "hypothesis.proposed":
            claim = CodexAdapter._extract_transport_text(payload, "claim", "statement")
            entity_id = CodexAdapter._build_entity_id("hyp", worker_input, event_type, payload, index)
            hypothesis_payload: dict[str, Any] = {
                "id": entity_id,
                "audit_id": audit_id,
                "status": "proposed",
                "statement": claim,
                "rationale": payload.get("rationale") or "Derived from transport-layer evidence.",
                "confidence": payload.get("confidence") or "medium",
                "origin": payload.get("origin") or "reader_inference",
                "created_at": occurred_at,
                "updated_at": occurred_at,
            }
            if transport_source_refs:
                hypothesis_payload["supporting_source_refs"] = transport_source_refs
            return hypothesis_payload, "hypothesis", entity_id

        if event_type == "question.opened":
            prompt = CodexAdapter._extract_transport_text(payload, "question", "prompt")
            entity_id = CodexAdapter._build_entity_id("question", worker_input, event_type, payload, index)
            related_entity_refs = CodexAdapter._build_related_entity_refs(worker_input)
            return (
                {
                    "id": entity_id,
                    "audit_id": audit_id,
                    "status": "open",
                    "prompt": prompt,
                    "context": CodexAdapter._coerce_context(payload.get("context")),
                    "answer": None,
                    "related_entity_refs": related_entity_refs,
                    "created_at": occurred_at,
                    "updated_at": occurred_at,
                },
                "question",
                entity_id,
            )

        if event_type in {"observation.verified", "observation.rejected"}:
            target_observation_id = worker_input["task"]["target"]["value"]
            target_observation = json.loads(
                json.dumps(worker_input["relevant_observations"][target_observation_id])
            )
            claim = payload.get("claim")
            if isinstance(claim, str) and claim.strip():
                target_observation["statement"] = claim.strip()
            if transport_source_refs:
                target_observation["provenance"] = {"source_refs": transport_source_refs}

            # evidence_class: refine from verifier if explicit, else keep existing
            explicit_ec = payload.get("evidence_class")
            if explicit_ec and normalize_evidence_class(explicit_ec):
                target_observation["evidence_class"] = normalize_evidence_class(explicit_ec)
            elif transport_source_refs:
                target_observation["evidence_class"] = derive_observation_evidence_class(
                    transport_source_refs
                )
            # else: keep existing evidence_class

            # Preserve provenance fields (Invariant 1 & 2)
            # evidence_origin and pattern_match_ids are NOT re-derived
            # Only change if verifier explicitly overrides
            explicit_origin = payload.get("evidence_origin_override")
            if explicit_origin:
                target_observation["evidence_origin"] = explicit_origin
            # else: keep existing evidence_origin (sticky)

            # pattern_match_ids: preserve unless verifier explicitly prunes
            if "pattern_match_ids" in payload:
                target_observation["pattern_match_ids"] = payload["pattern_match_ids"]
            # else: keep existing pattern_match_ids (sticky)

            target_observation["status"] = "verified" if event_type.endswith("verified") else "rejected"
            target_observation["updated_at"] = occurred_at
            return target_observation, "observation", target_observation_id

        if event_type == "contradiction.registered":
            conflicting_entity_refs = CodexAdapter._extract_entity_refs(
                payload.get("conflicting_entity_refs")
            )
            if len(conflicting_entity_refs) < 2:
                raise CodexOutputError(
                    "Transport contradiction.registered requires at least two conflicting_entity_refs."
                )
            entity_id = CodexAdapter._build_entity_id(
                "contradiction",
                worker_input,
                event_type,
                payload,
                index,
            )
            return (
                {
                    "id": entity_id,
                    "audit_id": audit_id,
                    "status": "open",
                    "summary": CodexAdapter._extract_transport_text(payload, "summary", "claim"),
                    "conflicting_entity_refs": conflicting_entity_refs,
                    "source_refs": transport_source_refs,
                    "created_at": occurred_at,
                    "updated_at": occurred_at,
                },
                "contradiction",
                entity_id,
            )

        if event_type == "issue.proposed":
            entity_id = CodexAdapter._build_entity_id("issue", worker_input, event_type, payload, index)
            evidence_payload = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
            observation_ids = CodexAdapter._extract_string_list(
                evidence_payload.get("observation_ids") or payload.get("observation_ids")
            )
            if not observation_ids and worker_input["task"]["target"]["kind"] == "observation":
                observation_ids = [worker_input["task"]["target"]["value"]]

            question_ids = CodexAdapter._extract_string_list(
                evidence_payload.get("question_ids") or payload.get("question_ids")
            )
            for question_id in worker_input.get("open_questions", {}):
                if question_id not in question_ids:
                    question_ids.append(question_id)

            contradiction_ids = CodexAdapter._extract_string_list(
                evidence_payload.get("contradiction_ids") or payload.get("contradiction_ids")
            )

            evidence: dict[str, Any] = {"observation_ids": observation_ids}
            if question_ids:
                evidence["question_ids"] = question_ids
            if contradiction_ids:
                evidence["contradiction_ids"] = contradiction_ids

            severity = payload.get("severity")
            severity_rule_ref = payload.get("severity_rule_ref")
            if not isinstance(severity, str) or not severity.strip():
                severity = None
                severity_rule_ref = None

            return (
                {
                    "id": entity_id,
                    "audit_id": audit_id,
                    "status": "proposed",
                    "title": CodexAdapter._extract_transport_text(payload, "title"),
                    "summary": CodexAdapter._extract_transport_text(payload, "summary"),
                    "severity": severity,
                    "severity_rule_ref": severity_rule_ref,
                    "evidence": evidence,
                    "created_at": occurred_at,
                    "updated_at": occurred_at,
                },
                "issue",
                entity_id,
            )

        if event_type == "candidate.proposed":
            # CandidateGenerator emits non-authoritative candidate proposals.
            # These are speculative and require verification before becoming truth-bearing.
            entity_id = CodexAdapter._build_entity_id("candidate", worker_input, event_type, payload, index)

            # Extract candidate-specific fields
            candidate_type = payload.get("candidate_type")
            if not isinstance(candidate_type, str) or not candidate_type.strip():
                raise CodexOutputError(
                    "Transport candidate.proposed requires a non-empty candidate_type."
                )

            proposed_claim = payload.get("proposed_claim")
            if not isinstance(proposed_claim, str) or not proposed_claim.strip():
                raise CodexOutputError(
                    "Transport candidate.proposed requires a non-empty proposed_claim."
                )

            confidence = payload.get("confidence")
            if confidence not in ("high", "medium", "low"):
                raise CodexOutputError(
                    "Transport candidate.proposed confidence must be one of: high, medium, low."
                )

            # Build supporting evidence refs from transport source refs
            supporting_evidence_refs = []
            for source_ref in transport_source_refs:
                evidence_ref = {
                    "file_path": source_ref.get("file_path"),
                    "line_range": source_ref.get("line_range"),
                    "snapshot_ref": source_ref.get("snapshot_ref", snapshot_ref),
                }
                if "file_hash" in source_ref:
                    evidence_ref["file_hash"] = source_ref["file_hash"]
                supporting_evidence_refs.append(evidence_ref)

            candidate_payload: dict[str, Any] = {
                "id": entity_id,
                "audit_id": audit_id,
                "status": "proposed",
                "candidate_type": candidate_type,
                "proposed_claim": proposed_claim,
                "confidence": confidence,
                "supporting_evidence_refs": supporting_evidence_refs,
                "reasoning_basis": payload.get("reasoning_basis") or "Generated by CandidateGenerator worker.",
                "created_at": occurred_at,
                "updated_at": occurred_at,
            }

            # Add type-specific optional fields
            if candidate_type == "risk_candidate":
                risk_category = payload.get("risk_category")
                if risk_category:
                    candidate_payload["risk_category"] = risk_category
                severity_hint = payload.get("severity_hint")
                if severity_hint:
                    candidate_payload["severity_hint"] = severity_hint
                trigger_observation_ids = payload.get("trigger_observation_ids")
                if isinstance(trigger_observation_ids, list):
                    candidate_payload["trigger_observation_ids"] = trigger_observation_ids

            elif candidate_type == "policy_candidate":
                policy_rule_ref = payload.get("policy_rule_ref")
                if policy_rule_ref:
                    candidate_payload["policy_rule_ref"] = policy_rule_ref
                policy_category = payload.get("policy_category")
                if policy_category:
                    candidate_payload["policy_category"] = policy_category
                trigger_observation_ids = payload.get("trigger_observation_ids")
                if isinstance(trigger_observation_ids, list):
                    candidate_payload["trigger_observation_ids"] = trigger_observation_ids

            elif candidate_type == "cross_file_correlation":
                relationship_type = payload.get("relationship_type")
                if relationship_type:
                    candidate_payload["relationship_type"] = relationship_type
                involved_file_paths = payload.get("involved_file_paths")
                if isinstance(involved_file_paths, list):
                    candidate_payload["involved_file_paths"] = involved_file_paths
                related_observation_ids = payload.get("related_observation_ids")
                if isinstance(related_observation_ids, list):
                    candidate_payload["related_observation_ids"] = related_observation_ids

            elif candidate_type == "verification_target":
                verification_target = payload.get("verification_target")
                if isinstance(verification_target, dict):
                    candidate_payload["verification_target"] = verification_target
                verification_questions = payload.get("verification_questions")
                if isinstance(verification_questions, list):
                    candidate_payload["verification_questions"] = verification_questions
                trigger_observation_ids = payload.get("trigger_observation_ids")
                if isinstance(trigger_observation_ids, list):
                    candidate_payload["trigger_observation_ids"] = trigger_observation_ids

            return candidate_payload, "candidate", entity_id

        raise CodexOutputError(f"Unsupported transport event_type '{event_type}'.")

    @staticmethod
    def _find_overlapping_pattern_ids(
        pattern_matches: list[dict[str, Any]],
        source_refs: list[dict[str, Any]],
    ) -> list[str]:
        """Find pattern_match_ids whose location overlaps with the observation's source refs."""
        matched_ids: list[str] = []
        for pm in pattern_matches:
            pm_file = pm.get("file_path", "").replace("\\", "/")
            pm_start = pm.get("line_start", 0) or 0
            pm_end = pm.get("line_end", 0) or 0
            pm_id = pm.get("pattern_match_id", "")
            for sr in source_refs:
                sr_file = sr.get("file_path", "").replace("\\", "/")
                lr = sr.get("line_range") or {}
                sr_start = lr.get("start", 0) or 0
                sr_end = lr.get("end", 0) or 0
                if pm_file and sr_file and pm_file == sr_file:
                    if pm_start <= sr_end and pm_end >= sr_start:
                        if pm_id and pm_id not in matched_ids:
                            matched_ids.append(pm_id)
                        break
        return matched_ids

    @staticmethod
    def _resolve_observation_evidence_class(
        *,
        payload: dict[str, Any],
        source_refs: list[dict[str, Any]],
        fallback: Any = None,
    ) -> str:
        explicit = payload.get("evidence_class")
        if explicit is not None:
            normalized = normalize_evidence_class(explicit)
            if normalized is None:
                raise CodexOutputError(
                    "Observation transport payload evidence_class must be one of: "
                    "direct_code_fact, derived_structural_fact, inferred_hypothesis, "
                    "blocked_verification. Use evidence_origin for pattern provenance."
                )
            return normalized

        if source_refs:
            return derive_observation_evidence_class(source_refs)

        fallback_class = normalize_evidence_class(fallback)
        if fallback_class is not None:
            return fallback_class
        return "blocked_verification"

    @staticmethod
    def _coerce_context(value: Any) -> str | None:
        """Coerce context to string or None as required by the event schema."""
        if value is None:
            return None
        if isinstance(value, str):
            return value if value.strip() else None
        # Non-string context (e.g. dict from LLM) — serialize to JSON string
        import json as _json
        return _json.dumps(value, ensure_ascii=True)

    @staticmethod
    def _extract_transport_text(payload: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        joined = ", ".join(keys)
        raise CodexOutputError(f"Transport payload must include one of the text fields: {joined}.")

    @staticmethod
    def _extract_transport_source_refs(
        payload: dict[str, Any],
        default_snapshot_ref: str,
    ) -> list[dict[str, Any]]:
        provenance = payload.get("provenance")
        if isinstance(provenance, dict) and isinstance(provenance.get("source_refs"), list):
            return json.loads(json.dumps(provenance["source_refs"]))

        source_refs = payload.get("source_refs")
        if isinstance(source_refs, list):
            return json.loads(json.dumps(source_refs))

        # Fallback: build source_ref from top-level file_path if present
        file_path = payload.get("file_path")
        if isinstance(file_path, str) and file_path.strip():
            line_range = payload.get("line_range")
            ref: dict[str, Any] = {"file_path": file_path.strip(), "snapshot_ref": default_snapshot_ref}
            if isinstance(line_range, dict) and isinstance(line_range.get("start"), int) and isinstance(line_range.get("end"), int):
                ref["line_range"] = line_range
            else:
                ref["line_range"] = {"start": 1, "end": 1}
            return [ref]

        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            return []

        normalized_refs: list[dict[str, Any]] = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise CodexOutputError(
                    f"Transport evidence item at index {index} must be a JSON object."
                )
            file_path = item.get("file_path")
            if not isinstance(file_path, str) or not file_path.strip():
                raise CodexOutputError(
                    f"Transport evidence item at index {index} must include file_path."
                )
            line_start = item.get("line_start")
            line_end = item.get("line_end")
            # Normalize line numbers: accept int or string representation of int
            line_start = CodexAdapter._normalize_line_number(line_start, "line_start", index)
            line_end = CodexAdapter._normalize_line_number(line_end, "line_end", index)
            source_ref: dict[str, Any] = {
                "file_path": file_path,
                "line_range": {
                    "start": line_start,
                    "end": line_end,
                },
                "snapshot_ref": item.get("snapshot_ref") or default_snapshot_ref,
            }
            file_hash = item.get("file_hash")
            if isinstance(file_hash, str) and file_hash:
                source_ref["file_hash"] = file_hash
            excerpt = item.get("excerpt")
            if isinstance(excerpt, str) and excerpt:
                source_ref["excerpt"] = excerpt
            normalized_refs.append(source_ref)
        return normalized_refs

    @staticmethod
    def _normalize_line_number(value: Any, field_name: str, index: int) -> int:
        """Normalize a line number to an integer.

        Accepts:
        - Integer values (e.g., 42)
        - String representations of integers (e.g., "42")

        Rejects:
        - None
        - Non-numeric strings (e.g., "abc")
        - Float values (e.g., 42.5)
        - Values less than 1
        """
        if value is None:
            raise CodexOutputError(
                f"Transport evidence item at index {index} must include {field_name}."
            )
        if isinstance(value, int):
            if value < 1:
                raise CodexOutputError(
                    f"Transport evidence item at index {index} {field_name} must be >= 1, got {value}."
                )
            return value
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
                if parsed < 1:
                    raise CodexOutputError(
                        f"Transport evidence item at index {index} {field_name} must be >= 1, got {parsed}."
                    )
                return parsed
            except ValueError:
                raise CodexOutputError(
                    f"Transport evidence item at index {index} {field_name} must be an integer, got '{value}'."
                )
        raise CodexOutputError(
            f"Transport evidence item at index {index} {field_name} must be an integer, got {type(value).__name__}."
        )

    @staticmethod
    def _extract_entity_refs(value: Any) -> list[dict[str, str]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise CodexOutputError("Transport conflicting_entity_refs must be a JSON array.")
        normalized_refs: list[dict[str, str]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise CodexOutputError(
                    f"Transport conflicting_entity_refs item at index {index} must be a JSON object."
                )
            entity_type = item.get("entity_type")
            entity_id = item.get("entity_id")
            if not isinstance(entity_type, str) or not isinstance(entity_id, str):
                raise CodexOutputError(
                    f"Transport conflicting_entity_refs item at index {index} must include entity_type and entity_id."
                )
            normalized_refs.append({"entity_type": entity_type, "entity_id": entity_id})
        return normalized_refs

    @staticmethod
    def _build_related_entity_refs(worker_input: dict[str, Any]) -> list[dict[str, str]]:
        target = worker_input["task"]["target"]
        entity_type = target["kind"]
        if entity_type == "path" or entity_type == "module":
            return []
        if entity_type == "observation":
            return [{"entity_type": "observation", "entity_id": target["value"]}]
        return [{"entity_type": entity_type, "entity_id": target["value"]}]

    @staticmethod
    def _extract_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise CodexOutputError("Transport field must be a JSON array when present.")
        normalized: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise CodexOutputError(
                    f"Transport string list item at index {index} must be a non-empty string."
                )
            normalized.append(item)
        return normalized

    @staticmethod
    def _build_entity_id(
        prefix: str,
        worker_input: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        index: int,
    ) -> str:
        fingerprint = CodexAdapter._canonical_json(
            {
                "task_id": worker_input["task"]["id"],
                "event_type": event_type,
                "payload": payload,
                "index": index,
            }
        )
        digest = hashlib.sha256(fingerprint.encode("ascii")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _sha256_json(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("ascii")).hexdigest()

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _cleanup_output_path(output_path: str) -> None:
        path = Path(output_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    # =========================================================================
    # BackendAdapter Protocol Implementation
    # =========================================================================

    def get_capabilities(self) -> BackendCapabilities:
        """
        Return declared capabilities of the Codex backend.

        Codex CLI capabilities depend on sandbox mode:
        - read-only: file_read only
        - full-access: file_read, file_write, shell (with restrictions)
        """
        supports_file_write = self.sandbox_mode != "read-only"
        supports_shell = self.sandbox_mode != "read-only"

        return BackendCapabilities(
            # Execution capabilities
            supports_session_context=False,  # Each invocation is independent
            supports_agent_loop=True,  # Codex has internal agent loop
            supports_streaming=False,  # We collect complete output
            # Tool capabilities
            supports_file_read=True,
            supports_file_write=supports_file_write,
            supports_shell=supports_shell,
            supports_web=False,  # Codex doesn't have web tools by default
            # Output capabilities
            supports_structured_output_enforcement=True,  # Via --output-schema
            supports_tool_restriction=True,  # Via sandbox mode
            supports_model_override=True,  # Via --model flag
            # Metadata
            backend_type="codex",
            backend_version=self._get_codex_version(),
        )

    def check_capability_compatibility(
        self,
        required_capabilities: set[str],
    ) -> tuple[bool, list[str]]:
        """
        Check if backend supports required capabilities.

        Args:
            required_capabilities: Set of capability names required

        Returns:
            Tuple of (is_compatible, missing_capabilities)
        """
        capabilities = self.get_capabilities()
        missing: list[str] = []

        for cap_name in required_capabilities:
            cap_value = getattr(capabilities, cap_name, None)
            if cap_value is not True:
                missing.append(cap_name)

        return (len(missing) == 0, missing)

    def run_with_backend_contract(
        self,
        worker_role: str,
        worker_input: dict[str, Any],
        policy_envelope: BackendPolicyEnvelope,
    ) -> BackendInvocationResult:
        """
        Execute one worker invocation using the BackendAdapter contract.

        This method implements the BackendAdapter protocol, providing:
        - Policy validation before execution
        - Normalized BackendInvocationResult output
        - Telemetry collection
        - Failure classification

        Args:
            worker_role: Role of worker to execute
            worker_input: Worker-specific input data
            policy_envelope: Policy constraints for this invocation

        Returns:
            BackendInvocationResult with success/failure and telemetry
        """
        start_time = time.time()
        capabilities = self.get_capabilities()

        # Validate capability requirements
        required_caps = capabilities.get_required_capabilities(worker_role)
        is_compatible, missing_caps = self.check_capability_compatibility(required_caps)

        if not is_compatible:
            return BackendInvocationResult(
                success=False,
                error=BackendFailure(
                    kind=BackendFailureKind.CAPABILITY_MISMATCH,
                    message=f"Missing required capabilities: {', '.join(missing_caps)}",
                    outcome_level=OutcomeLevel.PROCESS,
                    retryable=False,
                    backend_type="codex",
                    worker_role=worker_role,
                    metadata={"missing_capabilities": missing_caps},
                ),
                telemetry=BackendTelemetry(
                    backend_type="codex",
                    policy_profile=policy_envelope.policy_profile_name,
                    outcome_level_reached=OutcomeLevel.PROCESS,
                ),
            )

        # Validate policy compatibility
        policy_error = self._validate_policy_compatibility(policy_envelope)
        if policy_error is not None:
            return BackendInvocationResult(
                success=False,
                error=policy_error,
                telemetry=BackendTelemetry(
                    backend_type="codex",
                    policy_profile=policy_envelope.policy_profile_name,
                    outcome_level_reached=OutcomeLevel.POLICY,
                ),
            )

        # Execute with timeout from policy
        timeout = policy_envelope.max_wall_clock_seconds or self.timeout_seconds

        try:
            result = self.run_with_result(worker_role, worker_input)

            duration = time.time() - start_time

            return BackendInvocationResult(
                success=True,
                payload=result.normalized_output,
                candidate_events=result.candidate_events,
                telemetry=BackendTelemetry(
                    backend_type="codex",
                    model=self.model,
                    duration_seconds=duration,
                    policy_profile=policy_envelope.policy_profile_name,
                    outcome_level_reached=OutcomeLevel.SEMANTIC,
                    metadata=result.invocation_metadata,
                ),
                input_digest=result.input_digest,
                output_digest=result.output_digest,
            )

        except CodexAdapterError as exc:
            duration = time.time() - start_time
            failure = self._classify_adapter_error(exc, worker_role)

            return BackendInvocationResult(
                success=False,
                error=failure,
                telemetry=BackendTelemetry(
                    backend_type="codex",
                    model=self.model,
                    duration_seconds=duration,
                    policy_profile=policy_envelope.policy_profile_name,
                    outcome_level_reached=failure.outcome_level,
                    metadata={
                        "failure_stage": getattr(exc, "failure_stage", None),
                        "input_digest": getattr(exc, "input_digest", None),
                    },
                ),
                input_digest=getattr(exc, "input_digest", None),
            )

        except Exception as exc:
            duration = time.time() - start_time
            failure = classify_exception(exc, backend_type="codex", worker_role=worker_role)

            return BackendInvocationResult(
                success=False,
                error=failure,
                telemetry=BackendTelemetry(
                    backend_type="codex",
                    model=self.model,
                    duration_seconds=duration,
                    policy_profile=policy_envelope.policy_profile_name,
                    outcome_level_reached=failure.outcome_level,
                    metadata={"raw_error": str(exc)},
                ),
            )

    def _validate_policy_compatibility(
        self,
        policy_envelope: BackendPolicyEnvelope,
    ) -> BackendFailure | None:
        """
        Validate that policy envelope is compatible with Codex backend.

        Returns None if compatible, or BackendFailure if incompatible.
        """
        capabilities = self.get_capabilities()

        # Check file read policy
        if policy_envelope.allow_file_read and not capabilities.supports_file_read:
            return BackendFailure(
                kind=BackendFailureKind.CAPABILITY_MISMATCH,
                message="Policy allows file_read but backend does not support it",
                outcome_level=OutcomeLevel.POLICY,
                retryable=False,
                backend_type="codex",
            )

        # Check file write policy
        if policy_envelope.allow_file_write and not capabilities.supports_file_write:
            return BackendFailure(
                kind=BackendFailureKind.CAPABILITY_MISMATCH,
                message="Policy allows file_write but backend sandbox mode prohibits it",
                outcome_level=OutcomeLevel.POLICY,
                retryable=False,
                backend_type="codex",
                metadata={"sandbox_mode": self.sandbox_mode},
            )

        # Check shell policy
        if policy_envelope.allow_shell and not capabilities.supports_shell:
            return BackendFailure(
                kind=BackendFailureKind.CAPABILITY_MISMATCH,
                message="Policy allows shell but backend sandbox mode prohibits it",
                outcome_level=OutcomeLevel.POLICY,
                retryable=False,
                backend_type="codex",
                metadata={"sandbox_mode": self.sandbox_mode},
            )

        return None

    def _classify_adapter_error(
        self,
        exc: CodexAdapterError,
        worker_role: str,
    ) -> BackendFailure:
        """
        Classify a CodexAdapterError into a BackendFailure.
        """
        failure_stage = getattr(exc, "failure_stage", "unknown")

        # Map failure stages to outcome levels and failure kinds
        stage_mapping = {
            # Process level
            "resolve_executable": (OutcomeLevel.PROCESS, BackendFailureKind.BACKEND_UNAVAILABLE),
            "invoke_codex": (OutcomeLevel.PROCESS, BackendFailureKind.PROCESS_CRASH),
            "resolve_worker": (OutcomeLevel.PROCESS, BackendFailureKind.CAPABILITY_MISMATCH),
            # Transport level
            "validate_worker_result": (OutcomeLevel.TRANSPORT, BackendFailureKind.EMPTY_OUTPUT),
            "parse_transport_output": (OutcomeLevel.TRANSPORT, BackendFailureKind.INVALID_JSON),
            "normalize_transport_output": (OutcomeLevel.TRANSPORT, BackendFailureKind.MALFORMED_ENVELOPE),
            # Schema level
            "parse_worker_output": (OutcomeLevel.SCHEMA, BackendFailureKind.SCHEMA_VIOLATION),
            "validate_worker_output": (OutcomeLevel.SCHEMA, BackendFailureKind.SCHEMA_VIOLATION),
            # Semantic level
            "build_invocation": (OutcomeLevel.SEMANTIC, BackendFailureKind.SEMANTIC_ERROR),
        }

        outcome_level, failure_kind = stage_mapping.get(
            failure_stage,
            (OutcomeLevel.PROCESS, BackendFailureKind.PROCESS_CRASH)
        )

        # Special handling for timeout
        if "timeout" in str(exc).lower():
            outcome_level = OutcomeLevel.PROCESS
            failure_kind = BackendFailureKind.TIMEOUT

        # Special handling for specific error types
        if isinstance(exc, CodexInvocationError):
            outcome_level = OutcomeLevel.PROCESS
            failure_kind = BackendFailureKind.PROCESS_CRASH
            if "not found" in str(exc).lower() or "could not be resolved" in str(exc).lower():
                failure_kind = BackendFailureKind.BACKEND_UNAVAILABLE
            elif "timed out" in str(exc).lower():
                failure_kind = BackendFailureKind.TIMEOUT

        elif isinstance(exc, CodexOutputError):
            outcome_level = OutcomeLevel.TRANSPORT
            failure_kind = BackendFailureKind.MALFORMED_ENVELOPE
            if "must be valid JSON" in str(exc):
                failure_kind = BackendFailureKind.INVALID_JSON
            elif "empty" in str(exc).lower():
                failure_kind = BackendFailureKind.EMPTY_OUTPUT

        elif isinstance(exc, UnsupportedWorkerRoleError):
            outcome_level = OutcomeLevel.PROCESS
            failure_kind = BackendFailureKind.CAPABILITY_MISMATCH

        # Determine retryability
        retryable = failure_kind in {
            BackendFailureKind.TIMEOUT,
            BackendFailureKind.PROCESS_CRASH,
            BackendFailureKind.BACKEND_UNAVAILABLE,
        }

        return BackendFailure(
            kind=failure_kind,
            message=str(exc),
            outcome_level=outcome_level,
            retryable=retryable,
            backend_type="codex",
            worker_role=worker_role,
            failure_stage=failure_stage,
            raw_error=str(exc),
            metadata={
                "input_digest": getattr(exc, "input_digest", None),
                "output_digest": getattr(exc, "output_digest", None),
                "prompt_digest": getattr(exc, "prompt_digest", None),
            },
        )

    def _get_codex_version(self) -> str | None:
        """Get the version of the Codex executable."""
        try:
            command = list(self._resolve_codex_command())
            command.append("--version")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0]
        except Exception:
            pass
        return None
