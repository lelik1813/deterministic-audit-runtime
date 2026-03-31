from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationIssue:
    validator: str
    code: str
    message: str
    rule_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    issues: tuple[ValidationIssue, ...] = ()

