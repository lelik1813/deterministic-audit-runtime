from __future__ import annotations

import json
from pathlib import Path

from runtime.validators.contradiction import ContradictionValidator
from runtime.validators.duplicate import DuplicateValidator
from runtime.validators.models import ValidationIssue, ValidationResult
from runtime.validators.schema import SchemaValidator
from runtime.validators.source_binding import SourceBindingValidator
from runtime.validators.transition import TransitionValidator


class ValidatorSuite:
    """Deterministic validator pipeline for candidate events."""

    def __init__(self, root_dir: str | Path, events_dir: str | Path = "events") -> None:
        self.schema_validator = SchemaValidator(root_dir)
        self.duplicate_validator = DuplicateValidator(root_dir, events_dir=events_dir)
        self.source_binding_validator = SourceBindingValidator()
        self.transition_validator = TransitionValidator(root_dir, events_dir=events_dir)
        self.contradiction_validator = ContradictionValidator(root_dir, events_dir=events_dir)

    def validate_event(self, event: dict) -> ValidationResult:
        normalized_event = json.loads(json.dumps(event))

        schema_issues = tuple(self.schema_validator.validate(normalized_event))
        if schema_issues:
            return ValidationResult(is_valid=False, issues=schema_issues)

        duplicate_issues = tuple(self.duplicate_validator.validate(normalized_event))
        if duplicate_issues:
            return ValidationResult(is_valid=False, issues=duplicate_issues)

        issues: list[ValidationIssue] = []
        issues.extend(self.source_binding_validator.validate(normalized_event))
        issues.extend(self.transition_validator.validate(normalized_event))
        issues.extend(self.contradiction_validator.validate(normalized_event))
        return ValidationResult(is_valid=not issues, issues=tuple(issues))

