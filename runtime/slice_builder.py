from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.canonicalization import normalize_display_text
from runtime.pattern_scanner import scan_target_sources
from runtime.secret_redaction import redact_slice

try:
    from runtime.snapshot import RepositorySnapshot, SnapshotFileNotFoundError
except ModuleNotFoundError:  # pragma: no cover
    from snapshot import RepositorySnapshot, SnapshotFileNotFoundError

try:
    from runtime.policies import AuditPolicy, PolicyStore
    from runtime.tasks import AuditTask, TaskQueueStore
except ModuleNotFoundError:  # pragma: no cover - allows direct script execution.
    from policies import AuditPolicy, PolicyStore
    from tasks import AuditTask, TaskQueueStore


SLICE_SCHEMA_VERSION = "1.0.0"
ROLE_BY_TASK_TYPE = {
    "module_scan": "Reader",
    "verify_claim": "Verifier",
    "compose_issue": "IssueComposer",
}
COMMON_CONSTRAINTS = {
    "context_source": "canonical_state_plus_task_only",
    "conversational_context_allowed": False,
    "full_state_injection_allowed": False,
    "structured_output_required": True,
    "facts_require_source_binding": True,
}
ROLE_CONSTRAINTS = {
    "module_scan": {
        "allowed_event_types": [
            "observation.proposed",
            "hypothesis.proposed",
            "question.opened",
        ],
        "forbidden_actions": [
            "issue creation",
            "severity assignment",
            "converting hypothesis to fact",
        ],
    },
    "verify_claim": {
        "allowed_event_types": [
            "observation.verified",
            "observation.rejected",
            "contradiction.registered",
            "hypothesis.sent_to_verification",
            "hypothesis.supported",
            "hypothesis.rejected",
            "question.opened",
        ],
        "forbidden_actions": [
            "issue creation",
            "unsupported claims",
            "inference without evidence",
        ],
    },
    "compose_issue": {
        "allowed_event_types": [
            "issue.proposed",
        ],
        "forbidden_actions": [
            "using unverified observations as facts",
            "creating unsupported claims",
        ],
    },
}


class SliceBuildError(Exception):
    """Raised when a worker slice cannot be derived deterministically."""


@dataclass(frozen=True)
class SliceResult:
    task_id: str
    slice_id: str
    slice_fingerprint: str
    snapshot_ref: str
    slice_path: Path


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _normalize(value: Any) -> Any:
    return json.loads(json.dumps(value))


DETERMINISTIC_TASK_TIMESTAMP = "1970-01-01T00:00:00Z"
DETERMINISTIC_TASK_STATUS = "running"


class MemorySliceBuilder:
    """Build deterministic worker slices from canonical state plus one explicit task."""

    WORKSPACE_CONFIG_NAME = "audit_config.json"

    def __init__(
        self,
        root_dir: str | Path,
        state_dir: str | Path = "state",
        queue_name: str = "task_queue.json",
        canonical_state_name: str = "canonical_state.json",
        slices_dir_name: str = "slices",
        policy: AuditPolicy | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.state_dir = (self.root_dir / state_dir).resolve()
        self.canonical_state_path = (self.state_dir / canonical_state_name).resolve()
        self.slices_dir = (self.state_dir / slices_dir_name).resolve()
        self.task_queue = TaskQueueStore(self.root_dir, state_dir=state_dir, queue_name=queue_name)

        self._policy = policy
        if self._policy is None:
            profile_name = self._load_workspace_policy_profile()
            policy_store = PolicyStore(self.root_dir)
            self._policy = policy_store.get_policy(profile_name)

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.slices_dir.mkdir(parents=True, exist_ok=True)

    def _load_workspace_policy_profile(self) -> str | None:
        """Load the policy profile name from the workspace config."""
        config_path = self.root_dir / self.WORKSPACE_CONFIG_NAME
        if not config_path.exists():
            return None
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
            return config.get("policy") if isinstance(config, dict) else None
        except (json.JSONDecodeError, OSError):
            return None

    def build_slice(
        self,
        task_id: str,
        *,
        canonical_state: dict[str, Any] | None = None,
        task: AuditTask | None = None,
        snapshot: RepositorySnapshot | None = None,
    ) -> dict[str, Any]:
        task_obj = self._load_task(task_id, task)
        state = _normalize(canonical_state) if canonical_state is not None else self._load_canonical_state()
        self._validate_state_shape(state)
        self._validate_audit_alignment(state, task_obj)

        relevant_observations = self._select_relevant_observations(task_obj, state)
        relevant_hypotheses = self._select_relevant_hypotheses(task_obj, state)
        target_paths = self._build_target_paths(task_obj, relevant_observations)
        target_sources = self._build_target_sources(task_obj, target_paths, snapshot)
        open_questions = self._select_open_questions(task_obj, state, relevant_observations)
        answered_questions = self._select_answered_questions(task_obj, state, relevant_observations)
        # v1.3 Step 4: Build contradiction candidates for hypothesis verification
        contradiction_candidates = self._build_contradiction_candidates(
            task_obj, state, relevant_hypotheses
        )
        constraints = self._build_constraints(task_obj.type)
        stable_task = self._build_stable_task_view(task_obj)
        policy_constraints = self._build_policy_constraints()

        base_slice = {
            "schema_version": SLICE_SCHEMA_VERSION,
            "task": stable_task,
            "worker_role": ROLE_BY_TASK_TYPE[task_obj.type],
            "snapshot_ref": task_obj.target.snapshot_ref,
            "target_paths": target_paths,
            "relevant_observations": relevant_observations,
            "relevant_hypotheses": relevant_hypotheses,
            "open_questions": open_questions,
            "answered_questions": answered_questions,
            "contradiction_candidates": contradiction_candidates,
            "constraints": constraints,
            "policy": policy_constraints,
        }

        slice_fingerprint = self._build_slice_fingerprint(base_slice)
        slice_id = self._build_slice_id(slice_fingerprint)
        result = {
            "schema_version": SLICE_SCHEMA_VERSION,
            "slice_id": slice_id,
            "slice_fingerprint": slice_fingerprint,
            "task": stable_task,
            "worker_role": ROLE_BY_TASK_TYPE[task_obj.type],
            "snapshot_ref": task_obj.target.snapshot_ref,
            "target_paths": target_paths,
            "relevant_observations": relevant_observations,
            "relevant_hypotheses": relevant_hypotheses,
            "open_questions": open_questions,
            "answered_questions": answered_questions,
            "contradiction_candidates": contradiction_candidates,
            "constraints": constraints,
            "policy": policy_constraints,
        }
        if target_sources:
            result["target_sources"] = target_sources
            # Deterministic pattern pre-scan: inject matches for Reader
            pattern_matches = scan_target_sources(
                target_sources, snapshot_ref=result.get("snapshot_ref", "")
            )
            if pattern_matches:
                result["pattern_matches"] = [
                    m.to_dict() for m in pattern_matches
                ]
        return result

    def _build_target_sources(
        self,
        task: AuditTask,
        target_paths: list[str],
        snapshot: RepositorySnapshot | None,
    ) -> list[dict[str, Any]]:
        """Build target_sources with file content from snapshot.

        Only populated for module_scan / path / module tasks when a snapshot
        is provided. Missing files are silently skipped.

        For directory targets, lists the files in the directory via git ls-tree
        and reads each file individually.
        """
        if snapshot is None:
            return []
        if task.type != "module_scan" or task.target.kind not in {"path", "module"}:
            return []

        sources: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for path in target_paths:
            # First try reading as a file
            try:
                content = snapshot.read_text(path)
                if isinstance(content, str) and not self._is_git_tree_listing(content):
                    entry = self._build_source_entry(snapshot, path, content)
                    if entry is not None and path not in seen_paths:
                        sources.append(entry)
                        seen_paths.add(path)
                    continue
            except (SnapshotFileNotFoundError, Exception):
                pass

            # File read failed or returned a directory listing — expand via git ls-tree
            dir_files = self._list_directory_files(snapshot, path)
            for file_path in dir_files:
                if file_path in seen_paths:
                    continue
                try:
                    content = snapshot.read_text(file_path)
                except (SnapshotFileNotFoundError, Exception):
                    continue
                if not isinstance(content, str):
                    continue
                entry = self._build_source_entry(snapshot, file_path, content)
                if entry is not None:
                    sources.append(entry)
                    seen_paths.add(file_path)

        return sources

    @staticmethod
    def _build_source_entry(
        snapshot: RepositorySnapshot,
        file_path: str,
        content: str,
    ) -> dict[str, Any] | None:
        """Build a single target_source entry."""
        entry: dict[str, Any] = {
            "file_path": file_path,
            "snapshot_ref": snapshot.snapshot_ref,
            "file_content": content,
        }
        try:
            entry["file_hash"] = snapshot.compute_file_hash(file_path)
        except (SnapshotFileNotFoundError, Exception):
            pass
        return entry

    @staticmethod
    def _is_git_tree_listing(content: str) -> bool:
        """Detect whether content is a git tree listing, not real file content.

        ``git show <ref>:<dir>`` succeeds and returns a listing like::

            tree <sha>:<dir>

            file1.py
            file2.py

        This is NOT real file content — it's a directory listing that the
        snapshot silently returns as a string. We must detect it and treat
        the path as a directory for expansion.
        """
        if not content.startswith("tree "):
            return False
        lines = content.split("\n", 2)
        if len(lines) < 3:
            return False
        # First line: "tree <sha>:<path>"
        first = lines[0]
        if ":" not in first:
            return False
        prefix, _ = first.split(":", 1)
        # "tree <sha>" is two tokens
        parts = prefix.split()
        return len(parts) == 2 and parts[0] == "tree"

    @staticmethod
    def _list_directory_files(
        snapshot: RepositorySnapshot,
        dir_path: str,
    ) -> list[str]:
        """List file paths under a directory in the snapshot using git ls-tree.

        Returns a flat list of file paths (not sub-directory paths).
        Recurses one level into subdirectories.
        """
        normalized = dir_path.replace("\\", "/").strip("/")
        try:
            output = RepositorySnapshot._git(
                snapshot.repo_root,
                "ls-tree",
                "-r",
                "--name-only",
                snapshot.snapshot_ref,
                "--",
                normalized,
            )
        except Exception:
            return []
        files: list[str] = []
        for line in output.strip().splitlines():
            line = line.strip()
            if line and not line.endswith("/"):
                files.append(line)
        return files

    def write_slice(
        self,
        task_id: str,
        *,
        canonical_state: dict[str, Any] | None = None,
        task: AuditTask | None = None,
        snapshot: RepositorySnapshot | None = None,
    ) -> SliceResult:
        slice_payload = self.build_slice(
            task_id, canonical_state=canonical_state, task=task, snapshot=snapshot
        )
        # Redact secrets before persistence
        redacted_payload = redact_slice(slice_payload)
        slice_path = (self.slices_dir / f"{task_id}.json").resolve()
        serialized = json.dumps(redacted_payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        slice_path.write_text(serialized, encoding="utf-8", newline="\n")
        return SliceResult(
            task_id=task_id,
            slice_id=redacted_payload["slice_id"],
            slice_fingerprint=redacted_payload["slice_fingerprint"],
            snapshot_ref=redacted_payload["snapshot_ref"],
            slice_path=slice_path,
        )

    def _load_task(self, task_id: str, task: AuditTask | None) -> AuditTask:
        if task is not None:
            if task.id != task_id:
                raise SliceBuildError(
                    f"Provided task id '{task.id}' does not match requested task id '{task_id}'."
                )
            return task

        stored_task = self.task_queue.get_task(task_id)
        if stored_task is None:
            raise SliceBuildError(f"Task '{task_id}' does not exist in the persisted task queue.")
        return stored_task

    def _load_canonical_state(self) -> dict[str, Any]:
        if not self.canonical_state_path.exists():
            raise SliceBuildError(f"Canonical state file does not exist: {self.canonical_state_path}")
        with self.canonical_state_path.open("r", encoding="utf-8") as handle:
            try:
                return json.load(handle)
            except json.JSONDecodeError as exc:
                raise SliceBuildError(
                    f"Canonical state is not valid JSON: {self.canonical_state_path}"
                ) from exc

    @staticmethod
    def _validate_state_shape(state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise SliceBuildError("Canonical state must be a JSON object.")

        required_maps = ("observations", "hypotheses", "questions", "issues", "contradictions")
        for field_name in required_maps:
            if not isinstance(state.get(field_name), dict):
                raise SliceBuildError(f"Canonical state field '{field_name}' must be an object map.")

    @staticmethod
    def _validate_audit_alignment(state: dict[str, Any], task: AuditTask) -> None:
        audit = state.get("audit")
        if audit is None:
            return
        if not isinstance(audit, dict):
            raise SliceBuildError("Canonical state 'audit' must be an object or null.")
        audit_id = audit.get("id")
        if audit_id != task.audit_id:
            raise SliceBuildError(
                f"Task audit id '{task.audit_id}' does not match canonical audit id '{audit_id}'."
            )

    def _select_relevant_observations(
        self,
        task: AuditTask,
        state: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        observations = state["observations"]

        if task.target.kind in {"path", "module"}:
            matched: dict[str, dict[str, Any]] = {}
            for observation_id, observation in sorted(observations.items()):
                if observation.get("audit_id") != task.audit_id:
                    continue
                if self._observation_matches_target_path(observation, task.target.value):
                    matched[observation_id] = self._normalize_observation_for_slice(observation)
            return matched

        if task.target.kind == "observation":
            observation = observations.get(task.target.value)
            if observation is None or observation.get("audit_id") != task.audit_id:
                raise SliceBuildError(
                    f"Task target observation '{task.target.value}' is missing from canonical state."
                )
            return {task.target.value: self._normalize_observation_for_slice(observation)}

        if task.target.kind == "issue":
            issue = state["issues"].get(task.target.value)
            if issue is None or issue.get("audit_id") != task.audit_id:
                raise SliceBuildError(
                    f"Task target issue '{task.target.value}' is missing from canonical state."
                )
            observation_ids = issue.get("evidence", {}).get("observation_ids", [])
            return self._collect_observations(observations, observation_ids, task.audit_id)

        if task.target.kind == "question":
            question = state["questions"].get(task.target.value)
            if question is None or question.get("audit_id") != task.audit_id:
                raise SliceBuildError(
                    f"Task target question '{task.target.value}' is missing from canonical state."
                )
            observation_ids = [
                entity_ref["entity_id"]
                for entity_ref in question.get("related_entity_refs", [])
                if entity_ref.get("entity_type") == "observation"
            ]
            return self._collect_observations(observations, observation_ids, task.audit_id)

        if task.target.kind == "contradiction":
            contradiction = state["contradictions"].get(task.target.value)
            if contradiction is None or contradiction.get("audit_id") != task.audit_id:
                raise SliceBuildError(
                    f"Task target contradiction '{task.target.value}' is missing from canonical state."
                )
            observation_ids = [
                entity_ref["entity_id"]
                for entity_ref in contradiction.get("conflicting_entity_refs", [])
                if entity_ref.get("entity_type") == "observation"
            ]
            return self._collect_observations(observations, observation_ids, task.audit_id)

        if task.target.kind == "hypothesis":
            # v1.3 Step 2: Include supporting observations for hypothesis verification
            # v1.3 Step 4: Build comprehensive evidence set (max 10 nodes)
            hypotheses = state.get("hypotheses", {})
            hypothesis = hypotheses.get(task.target.value)
            if hypothesis is None or hypothesis.get("audit_id") != task.audit_id:
                raise SliceBuildError(
                    f"Task target hypothesis '{task.target.value}' is missing from canonical state."
                )

            # Collect evidence set from multiple sources
            evidence_observation_ids = set()

            # 1. Direct supporting observations
            evidence_observation_ids.update(hypothesis.get("supporting_observation_ids", []))

            # 2. Observations from verification_basis (if already verified)
            verification_basis = hypothesis.get("verification_basis")
            if isinstance(verification_basis, dict):
                evidence_observation_ids.update(verification_basis.get("supporting_observations", []))

            # 3. Observations from related hypotheses' supporting observations
            MAX_EVIDENCE_NODES = 10
            for related_field in ("supporting_hypothesis_ids", "contradicting_hypothesis_ids"):
                for hyp_id in hypothesis.get(related_field, []):
                    related_hyp = hypotheses.get(hyp_id)
                    if related_hyp is None or related_hyp.get("audit_id") != task.audit_id:
                        continue
                    evidence_observation_ids.update(related_hyp.get("supporting_observation_ids", []))
                    # Stop early if we've exceeded the evidence budget
                    if len(evidence_observation_ids) > MAX_EVIDENCE_NODES:
                        break
                if len(evidence_observation_ids) > MAX_EVIDENCE_NODES:
                    break

            # Bound to max evidence nodes
            bounded_ids = sorted(evidence_observation_ids)[:MAX_EVIDENCE_NODES]
            return self._collect_observations(observations, bounded_ids, task.audit_id)

        if task.target.kind == "audit":
            return {}

        raise SliceBuildError(f"Unsupported task target kind '{task.target.kind}'.")

    @staticmethod
    def _collect_observations(
        observations: dict[str, Any],
        observation_ids: list[str],
        audit_id: str,
    ) -> dict[str, dict[str, Any]]:
        collected: dict[str, dict[str, Any]] = {}
        for observation_id in sorted(set(observation_ids)):
            observation = observations.get(observation_id)
            if observation is None or observation.get("audit_id") != audit_id:
                continue
            collected[observation_id] = MemorySliceBuilder._normalize_observation_for_slice(observation)
        return collected

    def _select_relevant_hypotheses(
        self,
        task: AuditTask,
        state: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Select relevant hypotheses for a verify_claim task.

        v1.3 Step 2: When task target is a hypothesis, include that hypothesis
        in the slice for verification. Also include any related hypotheses that
        share supporting observations or source references.

        v1.3 Step 3: Also include hypotheses referenced via relationship fields:
        - supporting_hypothesis_ids
        - contradicting_hypothesis_ids

        Bounded to max 5 related hypotheses to prevent slice explosion.
        """
        hypotheses = state.get("hypotheses", {})
        if not isinstance(hypotheses, dict):
            hypotheses = {}

        MAX_RELATED_HYPOTHESES = 5  # Bounded to prevent slice explosion

        # For hypothesis verification tasks, include the target hypothesis
        if task.target.kind == "hypothesis":
            target_hypothesis = hypotheses.get(task.target.value)
            if target_hypothesis is None or target_hypothesis.get("audit_id") != task.audit_id:
                raise SliceBuildError(
                    f"Task target hypothesis '{task.target.value}' is missing from canonical state."
                )
            # Include the target hypothesis and any related hypotheses
            relevant = {task.target.value: self._normalize_hypothesis_for_slice(target_hypothesis)}

            # v1.3 Step 3: Include hypotheses referenced via relationship fields
            related_ids = set()
            related_ids.update(target_hypothesis.get("supporting_hypothesis_ids", []))
            related_ids.update(target_hypothesis.get("contradicting_hypothesis_ids", []))

            for hyp_id in sorted(related_ids):
                if len(relevant) >= MAX_RELATED_HYPOTHESES + 1:  # +1 for target
                    break
                hypothesis = hypotheses.get(hyp_id)
                if hypothesis is None or hypothesis.get("audit_id") != task.audit_id:
                    continue  # Skip missing or wrong-audit hypotheses
                relevant[hyp_id] = self._normalize_hypothesis_for_slice(hypothesis)

            # Also include hypotheses that share supporting observations
            target_obs_ids = set(target_hypothesis.get("supporting_observation_ids", []))
            for hyp_id, hypothesis in sorted(hypotheses.items()):
                if len(relevant) >= MAX_RELATED_HYPOTHESES + 1:
                    break
                if hyp_id == task.target.value:
                    continue
                if hyp_id in relevant:
                    continue  # Already included via relationship fields
                if hypothesis.get("audit_id") != task.audit_id:
                    continue
                if hypothesis.get("status") not in {"proposed", "in_verification"}:
                    continue  # Only include active hypotheses
                hyp_obs_ids = set(hypothesis.get("supporting_observation_ids", []))
                if target_obs_ids & hyp_obs_ids:  # Intersection
                    relevant[hyp_id] = self._normalize_hypothesis_for_slice(hypothesis)

            return relevant

        # For other task types, include hypotheses related to the target
        if task.target.kind in {"path", "module"}:
            relevant: dict[str, dict[str, Any]] = {}
            target_scope = self._normalize_path(task.target.value)
            for hyp_id, hypothesis in sorted(hypotheses.items()):
                if hypothesis.get("audit_id") != task.audit_id:
                    continue
                if hypothesis.get("status") not in {"proposed", "in_verification"}:
                    continue
                # Check if hypothesis sources overlap with target path
                for source_ref in hypothesis.get("supporting_source_refs", []):
                    file_path = source_ref.get("file_path")
                    if not isinstance(file_path, str):
                        continue
                    normalized_path = self._normalize_path(file_path)
                    if normalized_path == target_scope or normalized_path.startswith(f"{target_scope}/"):
                        relevant[hyp_id] = self._normalize_hypothesis_for_slice(hypothesis)
                        break
            return relevant

        # For observation, issue, question targets, include related hypotheses
        if task.target.kind in {"observation", "issue", "question"}:
            relevant: dict[str, dict[str, Any]] = {}
            for hyp_id, hypothesis in sorted(hypotheses.items()):
                if hypothesis.get("audit_id") != task.audit_id:
                    continue
                if hypothesis.get("status") not in {"proposed", "in_verification"}:
                    continue
                # Check if target observation is in supporting observations
                supporting_obs_ids = set(hypothesis.get("supporting_observation_ids", []))
                if task.target.kind == "observation" and task.target.value in supporting_obs_ids:
                    relevant[hyp_id] = self._normalize_hypothesis_for_slice(hypothesis)
            return relevant

        return {}

    @staticmethod
    def _normalize_hypothesis_for_slice(hypothesis: dict[str, Any]) -> dict[str, Any]:
        """Normalize a hypothesis for inclusion in a worker slice."""
        normalized = _normalize(hypothesis)
        for field_name in ("statement", "rationale"):
            if isinstance(normalized.get(field_name), str):
                normalized[field_name] = normalize_display_text(normalized[field_name])

        normalized["supporting_source_refs"] = MemorySliceBuilder._normalize_source_refs(
            normalized.get("supporting_source_refs")
        )
        # v1.3 Step 3: Normalize hypothesis relationship arrays
        for field_name in ("supporting_hypothesis_ids", "contradicting_hypothesis_ids"):
            if field_name in normalized:
                normalized[field_name] = sorted(set(normalized.get(field_name, [])))

        # v1.3 Step 4: Normalize verification_basis for slice inclusion
        verification_basis = normalized.get("verification_basis")
        if isinstance(verification_basis, dict):
            normalized_verification_basis = {
                "supporting_observations": sorted(set(verification_basis.get("supporting_observations", []))),
                "supporting_hypotheses": sorted(set(verification_basis.get("supporting_hypotheses", []))),
                "missing_evidence": sorted(set(verification_basis.get("missing_evidence", []))),
            }
            # Normalize contradictions_detected
            contradictions = verification_basis.get("contradictions_detected", [])
            if isinstance(contradictions, list):
                normalized_contradictions = []
                for c in contradictions:
                    if isinstance(c, dict) and "contradicting_hypothesis_id" in c:
                        normalized_contradictions.append({
                            "contradicting_hypothesis_id": c["contradicting_hypothesis_id"],
                            "description": normalize_display_text(c.get("description", "")),
                        })
                normalized_verification_basis["contradictions_detected"] = sorted(
                    normalized_contradictions,
                    key=lambda x: x["contradicting_hypothesis_id"]
                )
            normalized["verification_basis"] = normalized_verification_basis

        return normalized

    @staticmethod
    def _observation_matches_target_path(observation: dict[str, Any], target_value: str) -> bool:
        target_scope = MemorySliceBuilder._normalize_path(target_value)
        for source_ref in observation.get("provenance", {}).get("source_refs", []):
            file_path = source_ref.get("file_path")
            if not isinstance(file_path, str):
                continue
            normalized_path = MemorySliceBuilder._normalize_path(file_path)
            if normalized_path == target_scope or normalized_path.startswith(f"{target_scope}/"):
                return True
        return False

    @staticmethod
    def _normalize_path(path_value: str) -> str:
        return path_value.replace("\\", "/").strip("/")

    def _build_target_paths(
        self,
        task: AuditTask,
        relevant_observations: dict[str, dict[str, Any]],
    ) -> list[str]:
        target_paths: set[str] = set()

        if task.target.kind in {"path", "module"}:
            target_paths.add(self._normalize_path(task.target.value))

        for observation in relevant_observations.values():
            for source_ref in observation.get("provenance", {}).get("source_refs", []):
                file_path = source_ref.get("file_path")
                if isinstance(file_path, str) and file_path:
                    target_paths.add(self._normalize_path(file_path))

        return sorted(target_paths)

    def _select_open_questions(
        self,
        task: AuditTask,
        state: dict[str, Any],
        relevant_observations: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        target_refs = {(task.target.kind, task.target.value)}
        relevant_observation_ids = set(relevant_observations)
        questions: dict[str, dict[str, Any]] = {}

        for question_id, question in sorted(state["questions"].items()):
            if question.get("audit_id") != task.audit_id or question.get("status") != "open":
                continue
            if self._question_is_relevant(question, target_refs, relevant_observation_ids):
                questions[question_id] = self._normalize_question_for_slice(question)

        return questions

    def _select_answered_questions(
        self,
        task: AuditTask,
        state: dict[str, Any],
        relevant_observations: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        target_refs = {(task.target.kind, task.target.value)}
        relevant_observation_ids = set(relevant_observations)
        questions: dict[str, dict[str, Any]] = {}

        for question_id, question in sorted(state["questions"].items()):
            if question.get("audit_id") != task.audit_id or question.get("status") != "answered":
                continue
            if self._question_is_relevant(question, target_refs, relevant_observation_ids):
                questions[question_id] = self._normalize_question_for_slice(question)

        return questions

    def _build_contradiction_candidates(
        self,
        task: AuditTask,
        state: dict[str, Any],
        relevant_hypotheses: dict[str, dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Build contradiction candidates for hypothesis verification (v1.3 Step 4).

        Identifies which contradicting hypotheses are:
        1. Present in the relevant_hypotheses slice
        2. Have status "supported" (only supported hypotheses can be active contradictions)

        Returns a list of contradiction candidate descriptions to help the LLM
        detect local contradictions without traversing the full hypothesis graph.

        Does NOT:
        - Traverse the full hypothesis graph
        - Resolve contradictions
        - Score or rank contradictions
        """
        if task.target.kind != "hypothesis":
            return []

        hypotheses = state.get("hypotheses", {})
        target_hypothesis = hypotheses.get(task.target.value)
        if target_hypothesis is None:
            return []

        # Get contradicting_hypothesis_ids from target hypothesis
        contradicting_ids = set(target_hypothesis.get("contradicting_hypothesis_ids", []))
        if not contradicting_ids:
            return []

        # Find which contradicting hypotheses are present and supported
        candidates: list[dict[str, str]] = []
        for hyp_id in sorted(contradicting_ids):
            # Check if the contradicting hypothesis is in the slice
            if hyp_id not in relevant_hypotheses:
                continue

            # Check if the contradicting hypothesis is supported
            contradicting_hyp = hypotheses.get(hyp_id)
            if contradicting_hyp is None:
                continue
            if contradicting_hyp.get("status") != "supported":
                continue

            # Build candidate description
            statement = contradicting_hyp.get("statement", "")
            candidates.append({
                "hypothesis_id": hyp_id,
                "status": "supported",
                "statement_summary": statement[:200] if len(statement) > 200 else statement,
                "relationship": "contradicts_target",
            })

        return candidates

    @staticmethod
    def _question_is_relevant(
        question: dict[str, Any],
        target_refs: set[tuple[str, str]],
        relevant_observation_ids: set[str],
    ) -> bool:
        related_refs = {
            (entity_ref.get("entity_type"), entity_ref.get("entity_id"))
            for entity_ref in question.get("related_entity_refs", [])
        }

        if ("question", question.get("id")) in target_refs:
            return True

        for target_kind, target_value in target_refs:
            entity_type = "observation" if target_kind == "observation" else target_kind
            if (entity_type, target_value) in related_refs:
                return True

        for observation_id in relevant_observation_ids:
            if ("observation", observation_id) in related_refs:
                return True

        return False

    @staticmethod
    def _build_constraints(task_type: str) -> dict[str, Any]:
        role_constraints = ROLE_CONSTRAINTS[task_type]
        return {
            **COMMON_CONSTRAINTS,
            "allowed_event_types": sorted(role_constraints["allowed_event_types"]),
            "forbidden_actions": sorted(role_constraints["forbidden_actions"]),
        }

    def _build_policy_constraints(self) -> dict[str, Any]:
        """Build policy-derived constraints for the slice."""
        policy = self._policy
        return {
            "profile_name": policy.profile_name,
            "question_emission": {
                "emit_on_ambiguity": policy.question_emission.emit_on_ambiguity,
                "emit_on_security_concern": policy.question_emission.emit_on_security_concern,
                "emit_on_unclear_intent": policy.question_emission.emit_on_unclear_intent,
                "suppress_near_duplicates": policy.question_emission.suppress_near_duplicates,
                "max_questions_per_slice": policy.question_emission.max_questions_per_slice,
            },
            "issue_composition": {
                "require_rule_binding_for_severity": policy.issue_composition.require_rule_binding_for_severity,
                "include_low_confidence": policy.issue_composition.include_low_confidence,
                "min_evidence_class": policy.issue_composition.min_evidence_class,
            },
            "task_expansion": {
                "verify_claim_per_observation": policy.task_expansion.verify_claim_per_observation,
                "compose_issue_for_verified": policy.task_expansion.compose_issue_for_verified,
                "defer_on_budget_soft": policy.task_expansion.defer_on_budget_soft,
            },
        }

    @staticmethod
    def _build_slice_fingerprint(payload: dict[str, Any]) -> str:
        return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()

    @staticmethod
    def _build_slice_id(slice_fingerprint: str) -> str:
        return f"slice_{slice_fingerprint[:16]}"

    @staticmethod
    def _build_stable_task_view(task: AuditTask) -> dict[str, Any]:
        target = task.target.to_dict()
        if target["kind"] in {"path", "module"}:
            target["value"] = MemorySliceBuilder._normalize_path(target["value"])

        return {
            "id": task.id,
            "audit_id": task.audit_id,
            "type": task.type,
            "status": DETERMINISTIC_TASK_STATUS,
            "target": target,
            "attempt_count": 0,
            "last_error": None,
            "created_at": DETERMINISTIC_TASK_TIMESTAMP,
            "updated_at": DETERMINISTIC_TASK_TIMESTAMP,
        }

    @staticmethod
    def _normalize_observation_for_slice(observation: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize(observation)
        if isinstance(normalized.get("statement"), str):
            normalized["statement"] = normalize_display_text(normalized["statement"])

        provenance = normalized.get("provenance")
        if isinstance(provenance, dict):
            provenance["source_refs"] = MemorySliceBuilder._normalize_source_refs(
                provenance.get("source_refs")
            )
        return normalized

    @staticmethod
    def _normalize_question_for_slice(question: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize(question)
        for field_name in ("prompt", "context", "answer"):
            if isinstance(normalized.get(field_name), str):
                normalized[field_name] = normalize_display_text(normalized[field_name])
        normalized["related_entity_refs"] = MemorySliceBuilder._normalize_entity_refs(
            normalized.get("related_entity_refs")
        )
        return normalized

    @staticmethod
    def _normalize_source_refs(source_refs: Any) -> list[dict[str, Any]]:
        if not isinstance(source_refs, list):
            return []

        normalized_refs: list[dict[str, Any]] = []
        for source_ref in source_refs:
            if not isinstance(source_ref, dict):
                continue
            normalized_ref = _normalize(source_ref)
            file_path = normalized_ref.get("file_path")
            if isinstance(file_path, str):
                normalized_ref["file_path"] = MemorySliceBuilder._normalize_path(file_path)
            file_hash = normalized_ref.get("file_hash")
            if isinstance(file_hash, str) and file_hash:
                normalized_ref["file_hash"] = file_hash.lower()
            excerpt = normalized_ref.get("excerpt")
            if isinstance(excerpt, str):
                normalized_ref["excerpt"] = normalize_display_text(excerpt)
            normalized_refs.append(normalized_ref)

        return sorted(
            normalized_refs,
            key=lambda source_ref: (
                str(source_ref.get("file_path", "")),
                int(source_ref.get("line_range", {}).get("start", 0)),
                int(source_ref.get("line_range", {}).get("end", 0)),
                str(source_ref.get("snapshot_ref", "")),
                str(source_ref.get("file_hash", "")),
                str(source_ref.get("excerpt", "")),
            ),
        )

    @staticmethod
    def _normalize_entity_refs(entity_refs: Any) -> list[dict[str, Any]]:
        if not isinstance(entity_refs, list):
            return []

        normalized_refs = [
            _normalize(entity_ref)
            for entity_ref in entity_refs
            if isinstance(entity_ref, dict)
        ]
        return sorted(
            normalized_refs,
            key=lambda entity_ref: (
                str(entity_ref.get("entity_type", "")),
                str(entity_ref.get("entity_id", "")),
            ),
        )
