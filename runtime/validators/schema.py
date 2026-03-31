from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from runtime.validators.models import ValidationIssue


class SchemaValidator:
    name = "schema"

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve()
        self._validator = self._build_validator()

    def validate(self, event: dict[str, Any]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        errors = sorted(
            self._validator.iter_errors(event),
            key=lambda error: self._format_error_path(error),
        )
        issues.extend(
            ValidationIssue(
                validator=self.name,
                code="schema_validation_failed",
                message=self._format_validation_error(error),
                path=self._format_error_path(error) or None,
            )
            for error in errors
        )

        if issues:
            return issues

        payload = event["payload"]
        if payload["id"] != event["entity_id"]:
            issues.append(
                ValidationIssue(
                    validator=self.name,
                    code="payload_entity_id_mismatch",
                    message=(
                        f"Payload id '{payload['id']}' does not match entity id "
                        f"'{event['entity_id']}'."
                    ),
                    path="payload.id",
                )
            )

        if event["entity_type"] == "audit":
            if payload["id"] != event["audit_id"]:
                issues.append(
                    ValidationIssue(
                        validator=self.name,
                        code="payload_audit_id_mismatch",
                        message=(
                            f"Audit payload id '{payload['id']}' does not match audit id "
                            f"'{event['audit_id']}'."
                        ),
                        path="payload.id",
                    )
                )
        else:
            if payload.get("audit_id") != event["audit_id"]:
                issues.append(
                    ValidationIssue(
                        validator=self.name,
                        code="payload_audit_id_mismatch",
                        message=(
                            f"Payload audit id '{payload.get('audit_id')}' does not match audit id "
                            f"'{event['audit_id']}'."
                        ),
                        path="payload.audit_id",
                    )
                )

        return issues

    def _build_validator(self) -> Draft202012Validator:
        audit_schema_path = self.root_dir / "schema" / "audit.schema.json"
        event_schema_path = self.root_dir / "schema" / "event.schema.json"

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

    @staticmethod
    def _format_error_path(error: ValidationError) -> str:
        return ".".join(str(part) for part in error.absolute_path)

    @classmethod
    def _format_validation_error(cls, error: ValidationError) -> str:
        path = cls._format_error_path(error)
        if path:
            return f"{path}: {error.message}"
        return error.message

