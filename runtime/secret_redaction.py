"""Centralized secret redaction for audit artifacts.

This module provides deterministic secret detection and redaction across all
persistent audit artifacts. Secrets are replaced with stable placeholder tokens
while preserving artifact structure and semantic meaning.

Redaction is applied at serialization boundaries to ensure deterministic behavior.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# --- Configuration ---

# Minimum length for a string to be considered as a potential secret
MIN_SECRET_LENGTH = 16

# Minimum ratio of unique characters to total length for high-entropy detection
MIN_ENTROPY_RATIO = 0.35

# Fields that should NOT be redacted (they are structural identifiers)
SKIP_REDACTION_FIELDS = frozenset({
    "id", "audit_id", "task_id", "event_id", "observation_id", "issue_id",
    "question_id", "run_id", "slice_id", "entry_id", "entity_id",
    "projection_id", "report_id", "schema_version", "snapshot_ref",
    "file_hash", "input_digest", "output_digest", "prompt_digest",
    "raw_output_digest", "slice_fingerprint", "file_path", "target_repo_path",
    "repo_path", "ledger_path", "occurred_at", "created_at", "updated_at",
    "decided_at", "token",  # lock token, not secret
})

# Fields that are known to contain text where secrets might appear
TEXT_CONTENT_FIELDS = frozenset({
    "excerpt", "statement", "summary", "title", "prompt", "context",
    "answer", "rationale", "message", "error_message", "description",
    "content", "text", "code",
})

# Context indicators - suggest the string might be a secret
SECRET_CONTEXT_WORDS = frozenset({
    "password", "secret", "api_key", "token", "auth", "credential",
    "private_key", "apikey", "access_token", "bearer",
})


def _calculate_entropy_ratio(s: str) -> float:
    """Calculate the ratio of unique characters to string length."""
    if len(s) < MIN_SECRET_LENGTH:
        return 0.0
    unique_chars = len(set(s))
    return unique_chars / len(s)


def _looks_like_uuid(s: str) -> bool:
    """Check if string looks like a UUID format."""
    uuid_pattern = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
    return bool(re.match(uuid_pattern, s.lower()))


def _looks_like_hash(s: str) -> bool:
    """Check if string looks like a hash (hex only, fixed length)."""
    if not re.match(r'^[a-f0-9]+$', s.lower()):
        return False
    return len(s) in (32, 40, 64, 128)


def _looks_like_base64(s: str) -> bool:
    """Check if string looks like base64 encoded data."""
    if len(s) < 20:
        return False
    base64_pattern = r'^[A-Za-z0-9+/]+={0,2}$'
    if not re.match(base64_pattern, s):
        return False
    # Must be reasonable length and have some variety
    return len(set(s)) >= 8


def _has_secret_context(s: str, field_name: str) -> bool:
    """Check if context suggests this might be a secret."""
    s_lower = s.lower()
    field_lower = field_name.lower()

    for word in SECRET_CONTEXT_WORDS:
        if word in s_lower or word in field_lower:
            return True
    return False


def _detect_and_redact_secrets(s: str, field_name: str = "") -> str:
    """Detect secrets in a string and return a redacted version.

    This function applies various patterns to detect secrets and replaces
    them with stable placeholder tokens.
    """
    if not isinstance(s, str) or len(s) < MIN_SECRET_LENGTH:
        return s

    original = s
    result = s

    # Pattern 1: Anthropic-like API keys (sk-ant-...)
    anthropic_pattern = r'sk-ant-api03-[a-zA-Z0-9_\-]{80,}'
    if re.search(anthropic_pattern, s):
        result = re.sub(anthropic_pattern, '<REDACTED:ANTHROPIC_KEY>', result)

    # Pattern 2: OpenAI-like API keys (sk-...)
    openai_pattern = r'sk-[a-zA-Z0-9_\-]{20,}'
    if re.search(openai_pattern, s):
        result = re.sub(openai_pattern, '<REDACTED:OPENAI_KEY>', result)

    # Pattern 3: Bearer tokens
    bearer_pattern = r'[Bb]earer\s+[a-zA-Z0-9_\-\.]{20,}'
    if re.search(bearer_pattern, s):
        result = re.sub(bearer_pattern, 'Bearer <REDACTED_TOKEN>', result)

    # Pattern 4: Authorization headers
    auth_header_pattern = r'[Aa]uthorization\s*[:=]\s*["\']?[Bb]earer\s+[a-zA-Z0-9_\-\.]{20,}'
    if re.search(auth_header_pattern, s):
        result = re.sub(auth_header_pattern, 'Authorization: Bearer <REDACTED_TOKEN>', result)

    # Pattern 5: Password/secret assignments
    secret_assign_pattern = r'(password|secret|api_key|token|passwd|pwd)\s*[=:]\s*["\']([^"\']{8,})["\']'
    secret_match = re.search(secret_assign_pattern, s, re.IGNORECASE)
    if secret_match:
        var_type = secret_match.group(1).upper()
        result = re.sub(
            secret_assign_pattern,
            f'{secret_match.group(1)} = "<REDACTED_{var_type}>"',
            result,
            flags=re.IGNORECASE
        )

    # Pattern 6: Environment fallback with secrets (or "secret_value")
    env_fallback_pattern = r'or\s+["\']([a-zA-Z0-9_\-]{20,})["\']'
    env_match = re.search(env_fallback_pattern, s)
    if env_match and _has_secret_context(s, field_name):
        result = re.sub(env_fallback_pattern, 'or "<REDACTED_SECRET>"', result)

    # Pattern 7: Long high-entropy strings in secret context
    if result == original and _has_secret_context(s, field_name):
        # Find potential high-entropy strings that look like credentials
        # Must be quoted or after assignment operator
        high_entropy_pattern = r'["\']([a-zA-Z0-9_\-\.]{20,})["\']'
        for match in re.finditer(high_entropy_pattern, s):
            candidate = match.group(1)
            # Skip if looks like a variable name (snake_case with underscores)
            if re.match(r'^[a-z][a-z0-9]*(_[a-z][a-z0-9]*)+$', candidate):
                continue
            if _calculate_entropy_ratio(candidate) >= MIN_ENTROPY_RATIO:
                if not _looks_like_uuid(candidate):  # Don't redact UUIDs
                    # Create deterministic redaction marker
                    marker = f'<REDACTED:{hashlib.sha256(candidate.encode()).hexdigest()[:8]}>'
                    result = result.replace(candidate, marker, 1)

    # Pattern 7b: Unquoted high-entropy values after assignment in secret context
    if result == original and _has_secret_context(s, field_name):
        # Match: key_name = <high_entropy_value> (without quotes)
        assign_pattern = r'=\s*([a-zA-Z0-9_\-\.]{20,})(?:\s|$|,|;|\)|\])'
        for match in re.finditer(assign_pattern, s):
            candidate = match.group(1)
            # Skip if looks like a variable name (snake_case with underscores)
            if re.match(r'^[a-z][a-z0-9]*(_[a-z][a-z0-9]*)+$', candidate):
                continue
            if _calculate_entropy_ratio(candidate) >= MIN_ENTROPY_RATIO:
                if not _looks_like_uuid(candidate):  # Don't redact UUIDs
                    # Create deterministic redaction marker
                    marker = f'<REDACTED:{hashlib.sha256(candidate.encode()).hexdigest()[:8]}>'
                    result = result.replace(candidate, marker, 1)

    # Pattern 8: Base64-looking strings that might be credentials
    if result == original:
        base64_pattern = r'["\']([A-Za-z0-9+/]{40,}={0,2})["\']'
        for match in re.finditer(base64_pattern, s):
            candidate = match.group(1)
            if _looks_like_base64(candidate) and _has_secret_context(s, field_name):
                marker = f'<REDACTED:BASE64:{hashlib.sha256(candidate.encode()).hexdigest()[:8]}>'
                result = result.replace(candidate, marker, 1)

    # Pattern 9: Bare values after context words (without assignment operator)
    # E.g., "The password abc123def456ghi789jkl is hardcoded"
    if result == original:
        bare_context_pattern = r'\b(password|secret|api[_-]?key|token|credential)\s+([a-zA-Z0-9_\-\.]{16,})\b'
        for match in re.finditer(bare_context_pattern, s, re.IGNORECASE):
            candidate = match.group(2)
            # Skip if looks like a variable name (snake_case with underscores)
            if re.match(r'^[a-z][a-z0-9]*(_[a-z][a-z0-9]*)+$', candidate):
                continue
            if _calculate_entropy_ratio(candidate) >= MIN_ENTROPY_RATIO:
                if not _looks_like_uuid(candidate):
                    marker = f'<REDACTED:{hashlib.sha256(candidate.encode()).hexdigest()[:8]}>'
                    result = result.replace(candidate, marker, 1)

    return result


def should_redact_field(field_name: str) -> bool:
    """Determine if a field should be checked for secrets."""
    if field_name in SKIP_REDACTION_FIELDS:
        return False
    if field_name in TEXT_CONTENT_FIELDS:
        return True
    return False


def redact_string(s: str, field_name: str = "") -> str:
    """Redact secrets from a single string value."""
    if not isinstance(s, str) or not s:
        return s
    if field_name in SKIP_REDACTION_FIELDS:
        return s
    return _detect_and_redact_secrets(s, field_name)


def redact_value(value: Any, field_name: str = "") -> Any:
    """Recursively redact secrets from any value.

    Traverses dicts, lists, and nested structures, applying redaction
    to string fields that may contain secrets.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        if field_name in SKIP_REDACTION_FIELDS:
            return value
        # Always check strings for secrets (patterns will filter)
        return redact_string(value, field_name)

    if isinstance(value, dict):
        return {
            key: redact_value(item_value, key)
            for key, item_value in value.items()
        }

    if isinstance(value, list):
        # For list items, pass the field_name so nested text content is checked
        return [redact_value(item, field_name) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_value(item, field_name) for item in value)

    return value


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from an event payload before persistence."""
    if not isinstance(event, dict):
        return event
    return redact_value(event)


def redact_canonical_state(state: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from canonical state before persistence."""
    if not isinstance(state, dict):
        return state
    return redact_value(state)


def redact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from a report before persistence."""
    if not isinstance(report, dict):
        return report
    return redact_value(report)


def redact_slice(slice_payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from a worker slice before persistence."""
    if not isinstance(slice_payload, dict):
        return slice_payload
    return redact_value(slice_payload)


def redact_trace_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from a run ledger trace entry."""
    if not isinstance(entry, dict):
        return entry
    return redact_value(entry)
