from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from runtime.event_store import atomic_write_text
except ModuleNotFoundError:  # pragma: no cover - allows direct script execution.
    from event_store import atomic_write_text

try:
    from runtime.policies import AuditPolicy, PolicyStore, DEFAULT_PROFILE
except ModuleNotFoundError:  # pragma: no cover - allows direct script execution.
    from policies import AuditPolicy, PolicyStore, DEFAULT_PROFILE


QUEUE_SCHEMA_VERSION = "1.0.0"
TASK_TYPES = ("module_scan", "verify_claim", "compose_issue", "generate_candidates")
TASK_STATUSES = ("pending", "running", "done", "failed")
TARGET_KINDS = ("path", "module", "observation", "hypothesis", "issue", "question", "contradiction", "audit", "candidate")
TASK_PRIORITY = {
    "verify_claim": 0,
    "compose_issue": 1,
    "generate_candidates": 2,
    "module_scan": 3,
}

# Candidate routing configuration
CANDIDATE_TYPES = ("risk_candidate", "policy_candidate", "cross_file_correlation", "verification_target")
CANDIDATE_ROUTING_TARGET_KINDS = {
    "risk_candidate": "path",  # Route to verify the specific file/path
    "policy_candidate": "path",  # Route to verify the policy violation
    "cross_file_correlation": "path",  # Route to scan involved files
    "verification_target": "candidate",  # Route to verify the target entity
}
DEFAULT_CANDIDATE_ROUTING_BUDGET = {
    "max_verify_claim_per_candidate": 1,
    "max_module_scan_per_cross_file": 3,  # Limit files scanned for cross_file_correlation
    "max_total_tasks_per_candidate": 3,
    "defer_low_confidence": True,  # Don't route candidates with confidence="low"
}

TASK_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("running", "failed"),
    "running": ("done", "failed"),
    "failed": ("pending",),
    "done": (),
}
ACTIVE_QUEUE_STATUSES = frozenset({"pending", "running"})


class TaskQueueError(Exception):
    """Base error for task queue failures."""


class TaskTransitionError(TaskQueueError):
    """Raised when a task lifecycle change is not allowed."""


class TaskPlanningError(TaskQueueError):
    """Raised when the planner cannot derive explicit follow-up work."""


@dataclass(frozen=True)
class EnqueueResult:
    outcome: str
    task: "AuditTask"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _normalize(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _validate_timestamp(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise TaskQueueError(f"{name} must be a non-empty RFC 3339 timestamp.")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskQueueError(f"{name} must be a valid RFC 3339 timestamp.") from exc


def _validate_non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TaskQueueError(f"{name} must be a non-empty string.")


def _validate_line_range(line_range: dict[str, Any]) -> None:
    if not isinstance(line_range, dict):
        raise TaskQueueError("line_range must be an object when present.")
    if set(line_range.keys()) != {"start", "end"}:
        raise TaskQueueError("line_range must contain only 'start' and 'end'.")
    start = line_range["start"]
    end = line_range["end"]
    if not isinstance(start, int) or not isinstance(end, int):
        raise TaskQueueError("line_range values must be integers.")
    if start < 1 or end < start:
        raise TaskQueueError("line_range must satisfy 1 <= start <= end.")


def _normalize_target_value(kind: str, value: str) -> str:
    normalized = value.strip()
    if kind in {"path", "module"}:
        normalized = normalized.replace("\\", "/")
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        while "/./" in normalized:
            normalized = normalized.replace("/./", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.endswith("/") and normalized != "/":
            normalized = normalized.rstrip("/")
    return normalized


def _line_range_key(line_range: dict[str, int] | None) -> tuple[int, int]:
    if line_range is None:
        return (0, 0)
    return (line_range["start"], line_range["end"])


def _source_ref_key(source_ref: dict[str, Any]) -> tuple[Any, ...]:
    line_range = source_ref.get("line_range")
    line_start, line_end = (0, 0)
    if isinstance(line_range, dict):
        line_start, line_end = _line_range_key(line_range)
    return (
        _normalize_target_value("path", str(source_ref.get("file_path", ""))),
        line_start,
        line_end,
        str(source_ref.get("snapshot_ref", "")).strip(),
        str(source_ref.get("file_hash", "")).strip(),
    )


def _task_semantic_key(task: "AuditTask") -> tuple[Any, ...]:
    return (
        task.audit_id,
        task.type,
        task.target.kind,
        _normalize_target_value(task.target.kind, task.target.value),
        task.target.snapshot_ref,
        *_line_range_key(task.target.line_range),
    )


def _task_sort_key(task: "AuditTask") -> tuple[Any, ...]:
    return (
        TASK_PRIORITY.get(task.type, len(TASK_PRIORITY)),
        task.created_at,
        task.target.kind,
        _normalize_target_value(task.target.kind, task.target.value),
        task.target.snapshot_ref,
        *_line_range_key(task.target.line_range),
        task.id,
    )


@dataclass(frozen=True)
class TaskTarget:
    kind: str
    value: str
    snapshot_ref: str
    line_range: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.kind not in TARGET_KINDS:
            raise TaskQueueError(f"Unsupported task target kind '{self.kind}'.")
        # Normalize value at construction time for deterministic task_id
        normalized_value = _normalize_target_value(self.kind, self.value)
        object.__setattr__(self, "value", normalized_value)
        _validate_non_empty("target.value", self.value)
        # Normalize snapshot_ref (strip whitespace)
        normalized_snapshot_ref = self.snapshot_ref.strip()
        object.__setattr__(self, "snapshot_ref", normalized_snapshot_ref)
        _validate_non_empty("target.snapshot_ref", self.snapshot_ref)
        if self.line_range is not None:
            _validate_line_range(self.line_range)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "value": self.value,
            "snapshot_ref": self.snapshot_ref,
        }
        if self.line_range is not None:
            payload["line_range"] = dict(self.line_range)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskTarget":
        if not isinstance(payload, dict):
            raise TaskQueueError("task target must be an object.")
        allowed_keys = {"kind", "value", "snapshot_ref", "line_range"}
        extra_keys = set(payload.keys()) - allowed_keys
        if extra_keys:
            extras = ", ".join(sorted(extra_keys))
            raise TaskQueueError(f"task target contains unsupported fields: {extras}")
        return cls(
            kind=payload["kind"],
            value=payload["value"],
            snapshot_ref=payload["snapshot_ref"],
            line_range=payload.get("line_range"),
        )


@dataclass(frozen=True)
class AuditTask:
    id: str
    audit_id: str
    type: str
    status: str
    target: TaskTarget
    attempt_count: int
    last_error: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _validate_non_empty("task.id", self.id)
        if not self.id.startswith("task_"):
            raise TaskQueueError("task.id must start with 'task_'.")
        _validate_non_empty("task.audit_id", self.audit_id)
        if not self.audit_id.startswith("audit_"):
            raise TaskQueueError("task.audit_id must start with 'audit_'.")
        if self.type not in TASK_TYPES:
            raise TaskQueueError(f"Unsupported task type '{self.type}'.")
        if self.status not in TASK_STATUSES:
            raise TaskQueueError(f"Unsupported task status '{self.status}'.")
        if not isinstance(self.attempt_count, int) or self.attempt_count < 0:
            raise TaskQueueError("task.attempt_count must be a non-negative integer.")
        if self.last_error is not None:
            _validate_non_empty("task.last_error", self.last_error)
        _validate_timestamp("task.created_at", self.created_at)
        _validate_timestamp("task.updated_at", self.updated_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "type": self.type,
            "status": self.status,
            "target": self.target.to_dict(),
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def create(
        cls,
        audit_id: str,
        task_type: str,
        target: TaskTarget,
        *,
        created_at: str | None = None,
    ) -> "AuditTask":
        timestamp = created_at or _utc_now()
        return cls(
            id=build_task_id(audit_id=audit_id, task_type=task_type, target=target),
            audit_id=audit_id,
            type=task_type,
            status="pending",
            target=target,
            attempt_count=0,
            last_error=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AuditTask":
        if not isinstance(payload, dict):
            raise TaskQueueError("task payload must be an object.")
        required_keys = {
            "id",
            "audit_id",
            "type",
            "status",
            "target",
            "attempt_count",
            "last_error",
            "created_at",
            "updated_at",
        }
        if set(payload.keys()) != required_keys:
            missing = sorted(required_keys - set(payload.keys()))
            extra = sorted(set(payload.keys()) - required_keys)
            details: list[str] = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if extra:
                details.append(f"unsupported fields: {', '.join(extra)}")
            raise TaskQueueError(f"invalid task payload shape ({'; '.join(details)}).")
        return cls(
            id=payload["id"],
            audit_id=payload["audit_id"],
            type=payload["type"],
            status=payload["status"],
            target=TaskTarget.from_dict(payload["target"]),
            attempt_count=payload["attempt_count"],
            last_error=payload["last_error"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )


def build_task_id(audit_id: str, task_type: str, target: TaskTarget) -> str:
    fingerprint = _canonical_json(
        {
            "audit_id": audit_id,
            "type": task_type,
            "target": target.to_dict(),
        }
    )
    digest = hashlib.sha256(fingerprint.encode("ascii")).hexdigest()[:16]
    return f"task_{digest}"


class TaskQueueStore:
    """Persist and mutate an explicit task queue in state/task_queue.json."""

    def __init__(
        self,
        root_dir: str | Path,
        state_dir: str | Path = "state",
        queue_name: str = "task_queue.json",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.state_dir = (self.root_dir / state_dir).resolve()
        self.queue_path = (self.state_dir / queue_name).resolve()

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_queue_file()

    def ensure_queue_file(self) -> Path:
        if self.queue_path.exists():
            recovery = self.recover_queue()
            if recovery["repaired"]:
                # Log the repair but don't fail - tasks can be re-derived
                pass
            return self.queue_path

        self._write_tasks({})
        return self.queue_path

    def read_queue(self) -> dict[str, Any]:
        tasks = self._load_tasks()
        return {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "tasks": {task_id: task.to_dict() for task_id, task in sorted(tasks.items())},
        }

    def list_tasks(
        self,
        *,
        audit_id: str | None = None,
        task_type: str | None = None,
        status: str | None = None,
    ) -> list[AuditTask]:
        tasks = self._load_tasks().values()
        filtered = [
            task
            for task in tasks
            if (audit_id is None or task.audit_id == audit_id)
            and (task_type is None or task.type == task_type)
            and (status is None or task.status == status)
        ]
        return sorted(filtered, key=_task_sort_key)

    def get_task(self, task_id: str) -> AuditTask | None:
        return self._load_tasks().get(task_id)

    def claim_next_task(
        self,
        *,
        audit_id: str | None = None,
        task_type: str | None = None,
        updated_at: str | None = None,
    ) -> AuditTask | None:
        """Claim the next runnable task and persist its pending -> running transition."""
        pending_tasks = self.list_tasks(
            audit_id=audit_id,
            task_type=task_type,
            status="pending",
        )
        if not pending_tasks:
            return None

        next_task = pending_tasks[0]
        return self.transition_task(
            next_task.id,
            "running",
            updated_at=updated_at,
        )

    def enqueue_task(self, task: AuditTask) -> EnqueueResult:
        return self.enqueue_many([task])[0]

    def enqueue_many(self, tasks: Iterable[AuditTask]) -> list[EnqueueResult]:
        pending = sorted(tasks, key=_task_sort_key)
        stored = self._load_tasks()
        changed = False
        results: list[EnqueueResult] = []

        for task in pending:
            existing = stored.get(task.id)
            if existing is None:
                stored[task.id] = task
                changed = True
                results.append(EnqueueResult(outcome="enqueued", task=task))
                continue

            # Idempotent: if task with same id exists, return it as duplicate
            # But first validate invariant: task_id must map to consistent semantic identity
            if _task_semantic_key(existing) != _task_semantic_key(task):
                raise TaskQueueError(
                    f"Task id collision: '{task.id}' exists with different semantic content. "
                    f"existing={_task_semantic_key(existing)} "
                    f"incoming={_task_semantic_key(task)}"
                )
            results.append(EnqueueResult(outcome="duplicate", task=existing))

        if changed:
            self._write_tasks(stored)
        return results

    def transition_task(
        self,
        task_id: str,
        new_status: str,
        *,
        error: str | None = None,
        updated_at: str | None = None,
    ) -> AuditTask:
        if new_status not in TASK_STATUSES:
            raise TaskTransitionError(f"Unsupported task status '{new_status}'.")

        stored = self._load_tasks()
        current = stored.get(task_id)
        if current is None:
            raise TaskQueueError(f"Task '{task_id}' does not exist in {self.queue_path}.")

        allowed = TASK_TRANSITIONS[current.status]
        if new_status not in allowed:
            raise TaskTransitionError(
                f"Task '{task_id}' cannot transition from '{current.status}' to '{new_status}'."
            )

        if new_status == "failed":
            _validate_non_empty("error", error or "")

        timestamp = updated_at or _utc_now()
        attempt_count = current.attempt_count + 1 if new_status == "running" else current.attempt_count
        last_error = current.last_error

        if new_status == "failed":
            last_error = error.strip() if error is not None else None
        elif new_status in {"running", "done"}:
            last_error = None

        updated = AuditTask(
            id=current.id,
            audit_id=current.audit_id,
            type=current.type,
            status=new_status,
            target=current.target,
            attempt_count=attempt_count,
            last_error=last_error,
            created_at=current.created_at,
            updated_at=timestamp,
        )
        stored[task_id] = updated
        self._write_tasks(stored)
        return updated

    def _load_tasks(self) -> dict[str, AuditTask]:
        if not self.queue_path.exists():
            return {}

        with self.queue_path.open("r", encoding="utf-8") as handle:
            try:
                payload = json.load(handle)
            except json.JSONDecodeError as exc:
                raise TaskQueueError(
                    f"Task queue file is corrupted and cannot be parsed: {self.queue_path}: {exc}. "
                    "Call recover_queue() to reset to empty state, or delete the file manually. "
                    "Tasks can be re-derived from canonical state via TaskPlanner."
                ) from exc

        if not isinstance(payload, dict):
            raise TaskQueueError(
                "Task queue must be a JSON object. "
                f"Call recover_queue() to reset: {self.queue_path}"
            )
        if set(payload.keys()) != {"schema_version", "tasks"}:
            raise TaskQueueError(
                "Task queue must contain only 'schema_version' and 'tasks'. "
                f"Call recover_queue() to reset: {self.queue_path}"
            )
        if payload["schema_version"] != QUEUE_SCHEMA_VERSION:
            raise TaskQueueError(
                f"Unsupported task queue schema version '{payload['schema_version']}'. "
                f"Call recover_queue() to reset: {self.queue_path}"
            )
        if not isinstance(payload["tasks"], dict):
            raise TaskQueueError(
                "Task queue 'tasks' must be an object keyed by task id. "
                f"Call recover_queue() to reset: {self.queue_path}"
            )

        tasks: dict[str, AuditTask] = {}
        for task_id in sorted(payload["tasks"]):
            task = AuditTask.from_dict(payload["tasks"][task_id])
            if task.id != task_id:
                raise TaskQueueError(
                    f"Task entry '{task_id}' must contain the same id in its payload. "
                    f"Call recover_queue() to reset: {self.queue_path}"
                )
            tasks[task_id] = task
        return tasks

    def _write_tasks(self, tasks: dict[str, AuditTask]) -> None:
        payload = {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "tasks": {task_id: task.to_dict() for task_id, task in sorted(tasks.items())},
        }
        atomic_write_text(
            self.queue_path,
            json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        )

    def recover_queue(self) -> dict[str, Any]:
        """Validate and optionally repair the task queue file.

        Returns a recovery status dict with:
        - 'valid': bool - whether the queue was readable
        - 'repaired': bool - whether a repair was performed
        - 'message': str - description of what was found/done
        - 'task_count': int - number of tasks after recovery

        If the queue file is corrupted and cannot be parsed, it is reset
        to an empty queue. Tasks can be re-derived from canonical state via
        TaskPlanner.enqueue_follow_up_tasks().
        """
        if not self.queue_path.exists():
            return {
                "valid": True,
                "repaired": False,
                "message": "Queue file does not exist; will be created on first write.",
                "task_count": 0,
            }

        try:
            tasks = self._load_tasks()
            return {
                "valid": True,
                "repaired": False,
                "message": f"Queue file is valid with {len(tasks)} tasks.",
                "task_count": len(tasks),
            }
        except TaskQueueError as exc:
            # Corrupted file - reset to empty state
            # Tasks will be re-derived from canonical state by TaskPlanner
            self._write_tasks({})
            return {
                "valid": False,
                "repaired": True,
                "message": f"Queue file was corrupted and has been reset to empty: {exc}",
                "task_count": 0,
            }


class TaskPlanner:
    """Deterministically derive explicit work items from canonical state."""

    def __init__(
        self,
        root_dir: str | Path,
        state_dir: str | Path = "state",
        queue_name: str = "task_queue.json",
        canonical_state_name: str = "canonical_state.json",
        policy: AuditPolicy | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.state_dir = (self.root_dir / state_dir).resolve()
        self.canonical_state_path = (self.state_dir / canonical_state_name).resolve()
        self.queue_store = TaskQueueStore(self.root_dir, state_dir=state_dir, queue_name=queue_name)
        self._policy = policy
        if self._policy is None:
            policy_store = PolicyStore(self.root_dir)
            self._policy = policy_store.get_policy()
        if not isinstance(self._policy, AuditPolicy):
            raise TaskQueueError(f"TaskPlanner policy must be an AuditPolicy, got {type(self._policy)}")

    @property
    def policy(self) -> AuditPolicy:
        return self._policy

    def enqueue_initial_scan_tasks(
        self,
        audit_id: str,
        targets: Iterable[str],
        snapshot_ref: str,
        *,
        target_kind: str = "path",
        created_at: str | None = None,
    ) -> list[EnqueueResult]:
        if target_kind not in {"path", "module"}:
            raise TaskPlanningError("initial scan targets must use kind 'path' or 'module'.")

        unique_targets = sorted({target for target in targets if isinstance(target, str) and target})
        tasks = [
            AuditTask.create(
                audit_id=audit_id,
                task_type="module_scan",
                target=TaskTarget(kind=target_kind, value=target, snapshot_ref=snapshot_ref),
                created_at=created_at,
            )
            for target in unique_targets
        ]
        return self.queue_store.enqueue_many(tasks)

    def enqueue_follow_up_tasks(
        self,
        audit_id: str,
        canonical_state: dict[str, Any] | None = None,
        *,
        created_at: str | None = None,
    ) -> list[EnqueueResult]:
        state = _normalize(canonical_state) if canonical_state is not None else self._load_canonical_state()
        observations = state.get("observations")
        issues = state.get("issues")
        if not isinstance(observations, dict) or not isinstance(issues, dict):
            raise TaskPlanningError("canonical state must contain object maps for observations and issues.")

        audit_snapshot_ref = self._audit_snapshot_ref(state, audit_id)
        issue_backed_observation_ids = self._issue_backed_observation_ids(issues, audit_id)
        candidate_tasks: list[AuditTask] = []

        for observation in sorted(observations.values(), key=lambda item: item["id"]):
            if observation.get("audit_id") != audit_id:
                continue

            observation_id = observation["id"]
            snapshot_ref = self._observation_snapshot_ref(observation, audit_snapshot_ref)
            target = TaskTarget(
                kind="observation",
                value=observation_id,
                snapshot_ref=snapshot_ref,
            )

            if observation["status"] == "proposed":
                candidate_tasks.append(
                    AuditTask.create(
                        audit_id=audit_id,
                        task_type="verify_claim",
                        target=target,
                        created_at=created_at,
                    )
                )
                continue

            if observation["status"] == "verified" and observation_id not in issue_backed_observation_ids:
                candidate_tasks.append(
                    AuditTask.create(
                        audit_id=audit_id,
                        task_type="compose_issue",
                        target=target,
                        created_at=created_at,
                    )
                )

        return self._enqueue_follow_up_candidates(
            audit_id=audit_id,
            observations=observations,
            tasks=candidate_tasks,
        )

    def _load_canonical_state(self) -> dict[str, Any]:
        if not self.canonical_state_path.exists():
            raise TaskPlanningError(f"Canonical state file does not exist: {self.canonical_state_path}")
        with self.canonical_state_path.open("r", encoding="utf-8") as handle:
            try:
                return json.load(handle)
            except json.JSONDecodeError as exc:
                raise TaskPlanningError(
                    f"Canonical state is not valid JSON: {self.canonical_state_path}"
                ) from exc

    @staticmethod
    def _audit_snapshot_ref(state: dict[str, Any], audit_id: str) -> str | None:
        audit = state.get("audit")
        if not isinstance(audit, dict):
            return None
        if audit.get("id") != audit_id:
            return None
        snapshot_ref = audit.get("current_snapshot_ref")
        return snapshot_ref if isinstance(snapshot_ref, str) and snapshot_ref else None

    @staticmethod
    def _issue_backed_observation_ids(issues: dict[str, Any], audit_id: str) -> set[str]:
        observation_ids: set[str] = set()
        for issue in issues.values():
            if issue.get("audit_id") != audit_id:
                continue
            evidence = issue.get("evidence", {})
            observation_ids.update(evidence.get("observation_ids", []))
        return observation_ids

    @staticmethod
    def _observation_snapshot_ref(observation: dict[str, Any], fallback_snapshot_ref: str | None) -> str:
        provenance = observation.get("provenance", {})
        source_refs = provenance.get("source_refs") or []
        for source_ref in source_refs:
            snapshot_ref = source_ref.get("snapshot_ref")
            if isinstance(snapshot_ref, str) and snapshot_ref:
                return snapshot_ref
        if fallback_snapshot_ref is not None:
            return fallback_snapshot_ref
        raise TaskPlanningError(
            f"Observation '{observation.get('id', '<unknown>')}' does not expose a snapshot_ref."
        )

    def _enqueue_follow_up_candidates(
        self,
        *,
        audit_id: str,
        observations: dict[str, Any],
        tasks: list[AuditTask],
    ) -> list[EnqueueResult]:
        existing_tasks = self.queue_store.list_tasks(audit_id=audit_id)
        existing_by_semantic_key: dict[tuple[Any, ...], AuditTask] = {}
        for task in existing_tasks:
            semantic_key = self._follow_up_semantic_key(task, observations)
            existing_by_semantic_key.setdefault(semantic_key, task)

        # Track verify_claim budget
        verify_claim_active_total = 0
        verify_claim_active_by_observation: dict[str, int] = {}
        for task in existing_tasks:
            if task.type != "verify_claim" or task.status not in ACTIVE_QUEUE_STATUSES:
                continue
            verify_claim_active_total += 1
            if task.target.kind == "observation":
                obs_id = task.target.value
                verify_claim_active_by_observation[obs_id] = (
                    verify_claim_active_by_observation.get(obs_id, 0) + 1
                )

        # Track compose_issue budget
        compose_issue_active_total = 0
        compose_issue_active_by_source: dict[str, int] = {}
        for task in existing_tasks:
            if task.type != "compose_issue" or task.status not in ACTIVE_QUEUE_STATUSES:
                continue
            source_key = self._source_budget_key(task, observations)
            compose_issue_active_total += 1
            compose_issue_active_by_source[source_key] = (
                compose_issue_active_by_source.get(source_key, 0) + 1
            )

        # Use policy-based budgets
        verify_max_per_audit = self._policy.verify_claim_budget.max_per_audit
        verify_max_per_observation = self._policy.verify_claim_budget.max_per_observation
        verify_max_follow_up = self._policy.verify_claim_budget.max_follow_up_per_iteration
        compose_max_per_audit = self._policy.compose_issue_budget.max_per_audit
        compose_max_per_source_path = self._policy.compose_issue_budget.max_per_source_path
        defer_on_budget_soft = self._policy.task_expansion.defer_on_budget_soft

        results: list[EnqueueResult] = []
        planned_by_semantic_key: dict[tuple[Any, ...], AuditTask] = {}
        enqueued_verify_claim_this_iteration = 0

        for task in sorted(tasks, key=_task_sort_key):
            semantic_key = self._follow_up_semantic_key(task, observations)
            existing = existing_by_semantic_key.get(semantic_key)
            if existing is not None and existing.id != task.id:
                results.append(EnqueueResult(outcome="suppressed_near_duplicate", task=task))
                continue

            planned = planned_by_semantic_key.get(semantic_key)
            if planned is not None and planned.id != task.id:
                results.append(EnqueueResult(outcome="suppressed_near_duplicate", task=task))
                continue

            # Verify claim budget checks
            if task.type == "verify_claim":
                # Fan-out limit per iteration
                if enqueued_verify_claim_this_iteration >= verify_max_follow_up:
                    results.append(EnqueueResult(outcome="deferred_guardrail", task=task))
                    continue
                # Max per audit
                if verify_claim_active_total >= verify_max_per_audit:
                    results.append(EnqueueResult(outcome="deferred_guardrail", task=task))
                    continue
                # Max per observation (deduplication)
                if task.target.kind == "observation":
                    obs_id = task.target.value
                    if verify_claim_active_by_observation.get(obs_id, 0) >= verify_max_per_observation:
                        results.append(EnqueueResult(outcome="suppressed_near_duplicate", task=task))
                        continue

            # Compose issue budget checks
            if task.type == "compose_issue":
                source_key = self._source_budget_key(task, observations)
                if compose_issue_active_total >= compose_max_per_audit:
                    results.append(EnqueueResult(outcome="deferred_guardrail", task=task))
                    continue
                if (
                    compose_issue_active_by_source.get(source_key, 0)
                    >= compose_max_per_source_path
                ):
                    results.append(EnqueueResult(outcome="deferred_guardrail", task=task))
                    continue

            result = self.queue_store.enqueue_task(task)
            results.append(result)
            planned_by_semantic_key[semantic_key] = result.task

            if result.outcome == "enqueued":
                if task.type == "verify_claim":
                    enqueued_verify_claim_this_iteration += 1
                    verify_claim_active_total += 1
                    if task.target.kind == "observation":
                        obs_id = task.target.value
                        verify_claim_active_by_observation[obs_id] = (
                            verify_claim_active_by_observation.get(obs_id, 0) + 1
                        )
                elif task.type == "compose_issue":
                    source_key = self._source_budget_key(task, observations)
                    compose_issue_active_total += 1
                    compose_issue_active_by_source[source_key] = (
                        compose_issue_active_by_source.get(source_key, 0) + 1
                    )

        return results

    @staticmethod
    def _source_budget_key(task: AuditTask, observations: dict[str, Any]) -> str:
        if task.target.kind == "observation":
            observation = observations.get(task.target.value)
            if isinstance(observation, dict):
                provenance = observation.get("provenance", {})
                source_refs = provenance.get("source_refs") or []
                for source_ref in source_refs:
                    file_path = source_ref.get("file_path")
                    if isinstance(file_path, str) and file_path.strip():
                        return _normalize_target_value("path", file_path)
        return _normalize_target_value(task.target.kind, task.target.value)

    @staticmethod
    def _follow_up_semantic_key(task: AuditTask, observations: dict[str, Any]) -> tuple[Any, ...]:
        if task.target.kind != "observation":
            return _task_semantic_key(task)

        observation = observations.get(task.target.value)
        if not isinstance(observation, dict):
            return _task_semantic_key(task)

        statement = observation.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            return _task_semantic_key(task)

        provenance = observation.get("provenance", {})
        source_refs = provenance.get("source_refs") or []
        normalized_source_refs = tuple(
            sorted(
                _source_ref_key(source_ref)
                for source_ref in source_refs
                if isinstance(source_ref, dict)
            )
        )
        if not normalized_source_refs:
            return _task_semantic_key(task)

        return (
            task.audit_id,
            task.type,
            "observation_semantic",
            statement.strip(),
            observation.get("evidence_class"),
            normalized_source_refs,
        )


class CandidateRoutingError(TaskQueueError):
    """Raised when candidate routing fails."""


# Guardrail outcomes for explicit suppression tracking
CANDIDATE_ROUTING_OUTCOMES = frozenset({
    "enqueued",
    "duplicate",
    "deferred_low_confidence",
    "deferred_budget",
    "deferred_audit_limit",
    "deferred_run_limit",
    "suppressed_near_duplicate_file",
    "suppressed_type_disabled",
    "already_processed",
    "invalid_type",
    "no_evidence_refs",
    "no_snapshot_ref",
})


class CandidateRouter:
    """Routes accepted candidates into verification tasks with guardrails.

    Candidates are non-authoritative proposals that require verification
    before becoming truth-bearing. This class creates explicit follow-up
    verification tasks based on candidate type.

    CRITICAL: Candidates never become truth-bearing directly. They must
    go through verification to become observations first.

    Guardrails prevent unbounded task creation:
    - Per-audit candidate count limit
    - Per-run verify task limit
    - Per-candidate total task limit
    - File deduplication (suppress near-duplicate files)
    - Confidence filtering
    - Per-type routing toggles
    """

    def __init__(
        self,
        root_dir: str | Path,
        state_dir: str | Path = "state",
        queue_name: str = "task_queue.json",
        policy: AuditPolicy | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.state_dir = (self.root_dir / state_dir).resolve()
        self.queue_store = TaskQueueStore(self.root_dir, state_dir=state_dir, queue_name=queue_name)
        self._policy = policy
        if self._policy is None:
            policy_store = PolicyStore(self.root_dir)
            self._policy = policy_store.get_policy()
        if not isinstance(self._policy, AuditPolicy):
            raise TaskQueueError(f"CandidateRouter policy must be an AuditPolicy, got {type(self._policy)}")

    @property
    def policy(self) -> AuditPolicy:
        return self._policy

    @property
    def routing_policy(self):
        """Get the candidate routing policy from the audit policy."""
        return self._policy.candidate_routing

    def route_candidates_to_verification(
        self,
        audit_id: str,
        candidates: dict[str, Any],
        audit_snapshot_ref: str | None = None,
        *,
        created_at: str | None = None,
    ) -> list[EnqueueResult]:
        """Route accepted candidates into verification tasks with guardrails.

        This method creates explicit follow-up verification tasks
        for candidates based on their type. All suppression/deferral
        decisions are explicit with clear outcomes.

        Guardrails applied (in order):
        1. Per-audit candidate count limit
        2. Candidate status filter (must be 'proposed')
        3. Candidate type validation
        4. Confidence filter (defer low confidence if configured)
        5. Per-type routing toggle check
        6. Evidence refs validation
        7. Per-run verify task limit
        8. File deduplication
        9. Per-candidate task limit

        Args:
            audit_id: Audit ID to create tasks for
            candidates: Dict of candidate entities from canonical state
            audit_snapshot_ref: Default snapshot ref for the audit
            created_at: Optional timestamp for task creation

        Returns:
            List of EnqueueResult for each routing decision
        """
        if not isinstance(candidates, dict):
            raise CandidateRoutingError("candidates must be a dictionary")

        routing_policy = self.routing_policy
        routed_tasks: list[AuditTask] = []
        results: list[EnqueueResult] = []
        tasks_per_candidate: dict[str, int] = {}
        files_seen_this_run: set[str] = set()
        verify_tasks_this_run = 0
        candidates_this_audit = 0

        # Get existing tasks for this audit to count against limits
        existing_tasks = self.queue_store.list_tasks(audit_id=audit_id, status="pending")
        existing_verify_tasks = sum(1 for t in existing_tasks if t.type == "verify_claim")
        verify_tasks_this_run = existing_verify_tasks

        for candidate_id in sorted(candidates.keys()):
            candidate = candidates[candidate_id]

            # Skip candidates not in this audit
            if candidate.get("audit_id") != audit_id:
                continue

            # Guardrail 1: Per-audit candidate count limit
            candidates_this_audit += 1
            max_candidates = routing_policy.max_candidates_per_audit
            if candidates_this_audit > max_candidates:
                results.append(EnqueueResult(
                    outcome="deferred_audit_limit",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Guardrail 2: Skip candidates that are not in proposed status
            if candidate.get("status") != "proposed":
                results.append(EnqueueResult(
                    outcome="already_processed",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Guardrail 3: Candidate type validation
            candidate_type = candidate.get("candidate_type")
            if candidate_type not in CANDIDATE_TYPES:
                results.append(EnqueueResult(
                    outcome="invalid_type",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Guardrail 4: Confidence filter
            confidence = candidate.get("confidence")
            if confidence not in ("high", "medium", "low"):
                results.append(EnqueueResult(
                    outcome="invalid_type",  # Invalid confidence is a type error
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            if confidence == "low" and routing_policy.defer_low_confidence:
                results.append(EnqueueResult(
                    outcome="deferred_low_confidence",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Guardrail 5: Per-type routing toggle check
            type_enabled = self._check_type_routing_enabled(candidate_type, routing_policy)
            if not type_enabled:
                results.append(EnqueueResult(
                    outcome="suppressed_type_disabled",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Guardrail 6: Evidence refs validation
            primary_file = self._extract_primary_file(candidate)
            if not primary_file and candidate_type in ("risk_candidate", "policy_candidate"):
                results.append(EnqueueResult(
                    outcome="no_evidence_refs",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Guardrail 7: Per-run verify task limit
            max_verify = routing_policy.max_verify_tasks_per_run
            if verify_tasks_this_run >= max_verify:
                results.append(EnqueueResult(
                    outcome="deferred_run_limit",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Get snapshot ref for tasks
            try:
                snapshot_ref = self._get_candidate_snapshot_ref(candidate, audit_snapshot_ref)
            except CandidateRoutingError:
                results.append(EnqueueResult(
                    outcome="no_snapshot_ref",
                    task=self._create_placeholder_task(
                        audit_id=audit_id,
                        candidate_id=candidate_id,
                        audit_snapshot_ref=audit_snapshot_ref,
                        created_at=created_at,
                    ),
                ))
                continue

            # Route based on candidate type (with file deduplication)
            tasks_for_candidate = self._route_by_candidate_type(
                candidate_type=candidate_type,
                candidate=candidate,
                audit_id=audit_id,
                snapshot_ref=snapshot_ref,
                created_at=created_at,
                files_seen=files_seen_this_run,
                suppress_near_duplicates=routing_policy.suppress_near_duplicate_files,
            )

            # Guardrail 8: Per-candidate task limit
            max_per_candidate = routing_policy.max_total_tasks_per_candidate
            candidate_task_count = tasks_per_candidate.get(candidate_id, 0)

            for task in tasks_for_candidate:
                if candidate_task_count >= max_per_candidate:
                    results.append(EnqueueResult(
                        outcome="deferred_budget",
                        task=task,
                    ))
                    break

                # Track verify tasks against run limit
                if task.type == "verify_claim":
                    if verify_tasks_this_run >= max_verify:
                        results.append(EnqueueResult(
                            outcome="deferred_run_limit",
                            task=task,
                        ))
                        continue
                    verify_tasks_this_run += 1

                routed_tasks.append(task)
                candidate_task_count += 1

            tasks_per_candidate[candidate_id] = candidate_task_count

        # Enqueue all routed tasks
        if routed_tasks:
            enqueue_results = self.queue_store.enqueue_many(routed_tasks)
            results.extend(enqueue_results)

        return results

    @staticmethod
    def _check_type_routing_enabled(candidate_type: str, routing_policy) -> bool:
        """Check if routing is enabled for a specific candidate type."""
        type_toggles = {
            "risk_candidate": routing_policy.route_risk_candidates,
            "policy_candidate": routing_policy.route_policy_candidates,
            "cross_file_correlation": routing_policy.route_cross_file_correlations,
            "verification_target": routing_policy.route_verification_targets,
        }
        return type_toggles.get(candidate_type, False)

    def _route_by_candidate_type(
        self,
        candidate_type: str,
        candidate: dict[str, Any],
        audit_id: str,
        snapshot_ref: str,
        created_at: str | None,
        files_seen: set[str],
        suppress_near_duplicates: bool,
    ) -> list[AuditTask]:
        """Create tasks based on candidate type with file deduplication.

        Routing rules:
        - risk_candidate → verify_claim on primary file
        - policy_candidate → verify_claim on primary file
        - cross_file_correlation → module_scan on involved files (capped)
        - verification_target → verify_claim on target entity

        Args:
            candidate_type: Type of candidate
            candidate: Candidate entity
            audit_id: Audit ID
            snapshot_ref: Snapshot reference for the task
            created_at: Optional timestamp
            files_seen: Set of files already targeted in this run
            suppress_near_duplicates: Whether to suppress duplicate files

        Returns:
            List of AuditTask to enqueue (may be empty)
        """
        tasks: list[AuditTask] = []
        routing_policy = self.routing_policy

        if candidate_type == "risk_candidate":
            primary_file = self._extract_primary_file(candidate)
            if primary_file:
                # File deduplication check
                if suppress_near_duplicates and primary_file in files_seen:
                    return tasks
                files_seen.add(primary_file)

                target = TaskTarget(
                    kind="path",
                    value=primary_file,
                    snapshot_ref=snapshot_ref,
                )
                tasks.append(AuditTask.create(
                    audit_id=audit_id,
                    task_type="verify_claim",
                    target=target,
                    created_at=created_at,
                ))

        elif candidate_type == "policy_candidate":
            primary_file = self._extract_primary_file(candidate)
            if primary_file:
                # File deduplication check
                if suppress_near_duplicates and primary_file in files_seen:
                    return tasks
                files_seen.add(primary_file)

                target = TaskTarget(
                    kind="path",
                    value=primary_file,
                    snapshot_ref=snapshot_ref,
                )
                tasks.append(AuditTask.create(
                    audit_id=audit_id,
                    task_type="verify_claim",
                    target=target,
                    created_at=created_at,
                ))

        elif candidate_type == "cross_file_correlation":
            involved_files = candidate.get("involved_file_paths", [])
            max_files = routing_policy.max_module_scan_per_cross_file
            files_to_scan = involved_files[:max_files] if involved_files else []

            for file_path in files_to_scan:
                if isinstance(file_path, str) and file_path.strip():
                    normalized = _normalize_target_value("path", file_path)

                    # File deduplication check
                    if suppress_near_duplicates and normalized in files_seen:
                        continue
                    files_seen.add(normalized)

                    target = TaskTarget(
                        kind="path",
                        value=normalized,
                        snapshot_ref=snapshot_ref,
                    )
                    tasks.append(AuditTask.create(
                        audit_id=audit_id,
                        task_type="module_scan",
                        target=target,
                        created_at=created_at,
                    ))

        elif candidate_type == "verification_target":
            verification_target = candidate.get("verification_target", {})
            target_type = verification_target.get("target_type")
            target_id = verification_target.get("target_id")

            if target_type == "observation" and target_id:
                target = TaskTarget(
                    kind="observation",
                    value=target_id,
                    snapshot_ref=snapshot_ref,
                )
                tasks.append(AuditTask.create(
                    audit_id=audit_id,
                    task_type="verify_claim",
                    target=target,
                    created_at=created_at,
                ))
            # Note: hypothesis and candidate target types require additional context lookup

        return tasks

    @staticmethod
    def _extract_primary_file(candidate: dict[str, Any]) -> str | None:
        """Extract the primary file path from a candidate's evidence refs."""
        evidence_refs = candidate.get("supporting_evidence_refs", [])
        if not evidence_refs:
            return None

        # Get the first evidence ref with a file_path
        for ref in evidence_refs:
            file_path = ref.get("file_path")
            if isinstance(file_path, str) and file_path.strip():
                return _normalize_target_value("path", file_path)

        return None

    @staticmethod
    def _get_candidate_snapshot_ref(
        candidate: dict[str, Any],
        audit_snapshot_ref: str | None,
    ) -> str:
        """Get snapshot ref from candidate evidence, falling back to audit ref."""
        evidence_refs = candidate.get("supporting_evidence_refs", [])
        for ref in evidence_refs:
            snapshot_ref = ref.get("snapshot_ref")
            if isinstance(snapshot_ref, str) and snapshot_ref.strip():
                return snapshot_ref

        if audit_snapshot_ref:
            return audit_snapshot_ref

        raise CandidateRoutingError(
            f"Candidate '{candidate.get('id', '<unknown>')}' has no snapshot_ref "
            "and no audit fallback provided."
        )

    def _create_placeholder_task(
        self,
        audit_id: str,
        candidate_id: str,
        audit_snapshot_ref: str | None,
        created_at: str | None,
    ) -> AuditTask:
        """Create a placeholder task for routing result tracking."""
        return AuditTask(
            id=f"task_placeholder_{candidate_id}",
            audit_id=audit_id,
            type="verify_claim",
            status="pending",
            target=TaskTarget(
                kind="candidate",
                value=candidate_id,
                snapshot_ref=audit_snapshot_ref or "",
            ),
            attempt_count=0,
            last_error=None,
            created_at=created_at or _utc_now(),
            updated_at=created_at or _utc_now(),
        )

    def get_routing_summary(
        self,
        audit_id: str,
        candidates: dict[str, Any],
    ) -> dict[str, Any]:
        """Get a summary of routing decisions without creating tasks.

        Args:
            audit_id: Audit ID
            candidates: Dict of candidate entities

        Returns:
            Dict with routing summary including counts per outcome
        """
        summary = {
            "total_candidates": 0,
            "routed_to_verify": 0,
            "deferred_low_confidence": 0,
            "already_processed": 0,
            "invalid_type": 0,
            "no_evidence_refs": 0,
            "cross_file_tasks": 0,
        }

        for candidate_id, candidate in candidates.items():
            if candidate.get("audit_id") != audit_id:
                continue

            summary["total_candidates"] += 1

            status = candidate.get("status")
            if status != "proposed":
                summary["already_processed"] += 1
                continue

            candidate_type = candidate.get("candidate_type")
            if candidate_type not in CANDIDATE_TYPES:
                summary["invalid_type"] += 1
                continue

            confidence = candidate.get("confidence")
            if confidence == "low" and self._routing_budget.get("defer_low_confidence", True):
                summary["deferred_low_confidence"] += 1
                continue

            # Check for evidence refs
            primary_file = self._extract_primary_file(candidate)
            if not primary_file and candidate_type in ("risk_candidate", "policy_candidate"):
                summary["no_evidence_refs"] += 1
                continue

            if candidate_type == "cross_file_correlation":
                involved_files = candidate.get("involved_file_paths", [])
                max_files = self._routing_budget.get("max_module_scan_per_cross_file", 3)
                summary["cross_file_tasks"] += min(len(involved_files), max_files)
            else:
                summary["routed_to_verify"] += 1

        return summary


def enqueue_candidate_verification_tasks(
    root_dir: str | Path,
    audit_id: str,
    candidates: dict[str, Any],
    audit_snapshot_ref: str | None = None,
    *,
    state_dir: str | Path = "state",
    policy: AuditPolicy | None = None,
    created_at: str | None = None,
) -> list[EnqueueResult]:
    """Convenience function to route candidates into verification tasks.

    Args:
        root_dir: Root directory of the runtime
        audit_id: Audit ID
        candidates: Dict of candidate entities from canonical state
        audit_snapshot_ref: Default snapshot ref for the audit
        state_dir: State directory path
        policy: Optional audit policy
        created_at: Optional timestamp for task creation

    Returns:
        List of EnqueueResult for routed tasks
    """
    router = CandidateRouter(
        root_dir=root_dir,
        state_dir=state_dir,
        policy=policy,
    )
    return router.route_candidates_to_verification(
        audit_id=audit_id,
        candidates=candidates,
        audit_snapshot_ref=audit_snapshot_ref,
        created_at=created_at,
    )

