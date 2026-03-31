from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

try:
    from runtime.secret_redaction import redact_event
except ModuleNotFoundError:  # pragma: no cover - allows direct script execution.
    from secret_redaction import redact_event


class EventStoreError(Exception):
    """Base error for event store failures."""


class InvalidEventError(EventStoreError):
    """Raised when an event does not satisfy the JSON schema."""


class EventIdConflictError(EventStoreError):
    """Raised when an existing event id is reused with different content."""


class IdempotencyConflictError(EventStoreError):
    """Raised when an existing idempotency key is reused with different content."""


class LockAcquisitionError(EventStoreError):
    """Raised when an operational filesystem lock cannot be acquired safely."""


WORKSPACE_LOCK_NAME = ".workspace.lock"


@dataclass(frozen=True)
class AppendResult:
    outcome: str
    event_id: str
    line_number: int
    matched_on: str | None
    ledger_path: Path


@dataclass(frozen=True)
class StoredEvent:
    line_number: int
    event: dict[str, Any]


class FilesystemLock:
    """Filesystem lock with stale-lock cleanup and same-process reentrancy."""

    _HELD_LOCKS: dict[Path, dict[str, Any]] = {}

    def __init__(self, lock_path: str | Path, *, owner: str) -> None:
        self.lock_path = Path(lock_path).resolve()
        self.owner = owner
        self._token: str | None = None
        self._acquired = False

    def __enter__(self) -> "FilesystemLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        held = self._HELD_LOCKS.get(self.lock_path)
        if held is not None:
            held["count"] += 1
            self._token = held["token"]
            self._acquired = True
            return

        last_message = f"Lock is held: {self.lock_path}"
        for _ in range(2):
            token = self._build_token()
            payload = self._lock_payload(token)
            encoded = (
                json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                stale_cleared, last_message = self._clear_stale_lock()
                if stale_cleared:
                    continue
                raise LockAcquisitionError(last_message)

            try:
                self._write_all(fd, encoded)
                os.fsync(fd)
            except Exception:
                try:
                    os.close(fd)
                finally:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                raise
            else:
                os.close(fd)
                self._HELD_LOCKS[self.lock_path] = {"count": 1, "token": token}
                self._token = token
                self._acquired = True
                return

        raise LockAcquisitionError(last_message)

    def release(self) -> None:
        if not self._acquired:
            return

        held = self._HELD_LOCKS.get(self.lock_path)
        if held is None:
            self._acquired = False
            return

        held["count"] -= 1
        if held["count"] > 0:
            self._acquired = False
            return

        self._HELD_LOCKS.pop(self.lock_path, None)
        try:
            metadata = self._read_lock_payload()
        except EventStoreError:
            metadata = None

        if metadata is None or metadata.get("token") == self._token:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
        self._acquired = False

    def _clear_stale_lock(self) -> tuple[bool, str]:
        try:
            metadata = self._read_lock_payload()
        except EventStoreError:
            metadata = None

        if metadata is None:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                return True, f"Removed already-missing lock file: {self.lock_path}"
            return True, f"Removed unreadable lock file: {self.lock_path}"

        pid = metadata.get("pid")
        if isinstance(pid, int) and self._process_is_alive(pid):
            return False, self._busy_message(metadata)

        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return True, f"Removed already-missing stale lock file: {self.lock_path}"
        return True, f"Removed stale lock file: {self.lock_path}"

    def _read_lock_payload(self) -> dict[str, Any] | None:
        if not self.lock_path.exists():
            return None
        try:
            raw_text = self.lock_path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventStoreError(f"Lock file is unreadable: {self.lock_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise EventStoreError(f"Lock file must contain a JSON object: {self.lock_path}")
        return payload

    def _lock_payload(self, token: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "token": token,
            "owner": self.owner,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }

    def _build_token(self) -> str:
        return f"{os.getpid()}:{time.time_ns()}:{self.owner}"

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            import sys
            if sys.platform == "win32":
                # On Windows, os.kill(pid, 0) doesn't work reliably
                # Use psutil if available, otherwise use tasklist
                try:
                    import psutil
                    return psutil.pid_exists(pid)
                except ImportError:
                    import subprocess
                    result = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
                    )
                    return str(pid) in result.stdout
            else:
                # Unix-like systems
                os.kill(pid, 0)
                return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise EventStoreError("Lock write returned zero bytes.")
            written += count

    def _busy_message(self, metadata: dict[str, Any]) -> str:
        owner = metadata.get("owner", "<unknown>")
        pid = metadata.get("pid", "<unknown>")
        created_at = metadata.get("created_at", "<unknown>")
        return (
            f"Lock '{self.lock_path.name}' is held by owner '{owner}' "
            f"(pid={pid}, created_at={created_at})."
        )


def workspace_lock_path(root_dir: str | Path) -> Path:
    return (Path(root_dir).resolve() / WORKSPACE_LOCK_NAME).resolve()


def workspace_lock(root_dir: str | Path, *, owner: str) -> FilesystemLock:
    return FilesystemLock(workspace_lock_path(root_dir), owner=owner)


def atomic_write_text(path: str | Path, text: str) -> Path:
    target_path = Path(path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.tmp.",
        dir=str(target_path.parent),
    )
    temp_path = Path(temp_name)
    try:
        payload = text.encode("utf-8")
        try:
            FilesystemLock._write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_path, target_path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return target_path


class EventStore:
    """Append-only NDJSON event storage backed by the local filesystem."""

    def __init__(
        self,
        root_dir: str | Path,
        events_dir: str | Path = "events",
        ledger_name: str = "events.ndjson",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.events_dir = (self.root_dir / events_dir).resolve()
        self.ledger_path = (self.events_dir / ledger_name).resolve()
        self.lock_path = (self.events_dir / f"{ledger_name}.lock").resolve()
        self.schema_dir = (self.root_dir / "schema").resolve()

        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path.touch(exist_ok=True)
        self._validator = self._build_validator()

    def append_event(self, event: dict[str, Any]) -> AppendResult:
        """Validate and append one event unless it is a duplicate submission."""
        normalized_event = self._normalize_event(event)
        self._validate_event(normalized_event)
        # Redact secrets before serialization
        redacted_event = redact_event(normalized_event)
        serialized = self._serialize_event(redacted_event)
        with self._ledger_lock("event_store.append"):
            self._repair_trailing_partial_write()
            duplicate, last_line_number = self._find_duplicate_or_conflict(redacted_event, serialized)

            if duplicate is not None:
                return AppendResult(
                    outcome="duplicate",
                    event_id=duplicate.event["id"],
                    line_number=duplicate.line_number,
                    matched_on=self._match_reason(duplicate.event, normalized_event),
                    ledger_path=self.ledger_path,
                )

            self._append_serialized_line(serialized)
            return AppendResult(
                outcome="appended",
                event_id=normalized_event["id"],
                line_number=last_line_number + 1,
                matched_on=None,
                ledger_path=self.ledger_path,
            )

    def iter_events(self, audit_id: str | None = None) -> Iterable[dict[str, Any]]:
        """Yield events in append order, optionally filtered by audit id."""
        for stored_event in self.iter_stored_events(audit_id=audit_id):
            yield stored_event.event

    def iter_stored_events(self, audit_id: str | None = None) -> Iterable[StoredEvent]:
        """Yield stored events together with their ledger line numbers."""
        with self._ledger_lock("event_store.read"):
            self._repair_trailing_partial_write()
            with self.ledger_path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise EventStoreError(
                            f"Invalid NDJSON in {self.ledger_path} at line {line_number}: {exc}"
                        ) from exc
                    if audit_id is None or event.get("audit_id") == audit_id:
                        yield StoredEvent(line_number=line_number, event=event)

    def read_events(self, audit_id: str | None = None) -> list[dict[str, Any]]:
        """Return all stored events in append order."""
        return [stored_event.event for stored_event in self.iter_stored_events(audit_id=audit_id)]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one event by id if it exists in the ledger."""
        for stored_event in self.iter_stored_events():
            if stored_event.event["id"] == event_id:
                return stored_event.event
        return None

    def recover_ledger(self) -> Path:
        """Repair one recoverable trailing partial write, if present."""
        with self._ledger_lock("event_store.recover"):
            self._repair_trailing_partial_write()
        return self.ledger_path

    def has_idempotency_key(self, audit_id: str, idempotency_key: str) -> bool:
        """Return True when the ledger already contains the given idempotency key."""
        for stored_event in self.iter_stored_events(audit_id=audit_id):
            event = stored_event.event
            if event["idempotency_key"] == idempotency_key:
                return True
        return False

    def _ledger_lock(self, owner: str) -> FilesystemLock:
        return FilesystemLock(self.lock_path, owner=owner)

    def _build_validator(self) -> Draft202012Validator:
        audit_schema_path = self.schema_dir / "audit.schema.json"
        event_schema_path = self.schema_dir / "event.schema.json"

        with audit_schema_path.open("r", encoding="utf-8") as handle:
            audit_schema = json.load(handle)
        with event_schema_path.open("r", encoding="utf-8") as handle:
            event_schema = json.load(handle)

        Draft202012Validator.check_schema(audit_schema)
        Draft202012Validator.check_schema(event_schema)

        registry = Registry().with_resources(
            [
                (audit_schema["$id"], Resource.from_contents(audit_schema)),
                (event_schema["$id"], Resource.from_contents(event_schema)),
            ]
        )
        return Draft202012Validator(event_schema, registry=registry)

    def _validate_event(self, event: dict[str, Any]) -> None:
        errors = sorted(
            self._validator.iter_errors(event),
            key=lambda error: self._format_error_path(error),
        )
        if not errors:
            return

        formatted_errors = "; ".join(self._format_validation_error(error) for error in errors)
        raise InvalidEventError(formatted_errors)

    def _find_duplicate_or_conflict(
        self,
        candidate_event: dict[str, Any],
        candidate_serialized: str,
    ) -> tuple[StoredEvent | None, int]:
        last_line_number = 0
        for stored_event in self.iter_stored_events():
            last_line_number = stored_event.line_number
            existing_event = stored_event.event
            existing_serialized = self._serialize_event(existing_event)

            if existing_event["id"] == candidate_event["id"]:
                if existing_serialized == candidate_serialized:
                    return stored_event, last_line_number
                raise EventIdConflictError(
                    f"Event id '{candidate_event['id']}' already exists with different content."
                )

            same_audit = existing_event["audit_id"] == candidate_event["audit_id"]
            same_idempotency_key = (
                existing_event["idempotency_key"] == candidate_event["idempotency_key"]
            )
            if same_audit and same_idempotency_key:
                if existing_serialized == candidate_serialized:
                    return stored_event, last_line_number
                raise IdempotencyConflictError(
                    "Idempotency key "
                    f"'{candidate_event['idempotency_key']}' already exists for audit "
                    f"'{candidate_event['audit_id']}' with different content."
                )

        return None, last_line_number

    @staticmethod
    def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(event))

    @staticmethod
    def _serialize_event(event: dict[str, Any]) -> str:
        return json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _append_serialized_line(self, serialized: str) -> None:
        payload = (serialized + "\n").encode("utf-8")
        fd = os.open(
            str(self.ledger_path),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        )
        try:
            FilesystemLock._write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _repair_trailing_partial_write(self) -> None:
        raw_bytes = self.ledger_path.read_bytes()
        if not raw_bytes:
            return

        split_lines = raw_bytes.splitlines(keepends=True)
        if not split_lines:
            return

        offset = 0
        safe_size = len(raw_bytes)
        for index, raw_line in enumerate(split_lines):
            line_start = offset
            offset += len(raw_line)
            is_last_line = index == len(split_lines) - 1
            stripped = raw_line.strip()
            if not stripped:
                safe_size = offset
                continue
            try:
                decoded = stripped.decode("utf-8")
                payload = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if is_last_line:
                    self._truncate_ledger(line_start)
                    return
                raise EventStoreError(
                    f"Ledger contains non-recoverable invalid NDJSON before the tail at byte offset "
                    f"{line_start}: {exc}"
                ) from exc

            if not isinstance(payload, dict):
                if is_last_line:
                    self._truncate_ledger(line_start)
                    return
                raise EventStoreError(
                    f"Ledger line at byte offset {line_start} must decode to a JSON object."
                )
            safe_size = offset

        if raw_bytes.endswith(b"\n"):
            return

        last_line = split_lines[-1]
        if last_line.strip():
            try:
                decoded = last_line.strip().decode("utf-8")
                payload = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._truncate_ledger(safe_size - len(last_line))
                return
            if not isinstance(payload, dict):
                self._truncate_ledger(safe_size - len(last_line))

    def _truncate_ledger(self, size: int) -> None:
        with self.ledger_path.open("r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _match_reason(existing_event: dict[str, Any], candidate_event: dict[str, Any]) -> str | None:
        if existing_event["id"] == candidate_event["id"]:
            return "event_id"
        if (
            existing_event["audit_id"] == candidate_event["audit_id"]
            and existing_event["idempotency_key"] == candidate_event["idempotency_key"]
        ):
            return "idempotency_key"
        return None

    @staticmethod
    def _format_error_path(error: ValidationError) -> str:
        return ".".join(str(part) for part in error.absolute_path)

    @classmethod
    def _format_validation_error(cls, error: ValidationError) -> str:
        path = cls._format_error_path(error)
        if path:
            return f"{path}: {error.message}"
        return error.message
