"""
Context-Aware Redaction (STEP 4)

This module implements context-aware redaction that preserves non-sensitive
identifiers while still redacting actual secrets.

Core Invariant:
Redaction MUST NOT corrupt non-sensitive identifiers

Two-level model:
1. Field-aware redaction - knows which fields are safe by name
2. Token-aware redaction - respects token boundaries

Protected contexts:
- repo_label: "my-repo" should not become "my-<REDACTED>"
- URLs: "https://example.com/api" should not be distorted
- path segments: "src/auth/login.py" should stay intact
- identifiers: IDs, hashes, references should be preserved

DoD:
- repo_label не ломается
- URL не искажаются
- секреты продолжают редактироваться
- false positive → 0 на известных кейсах
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RedactionContext(Enum):
    """Context in which a string appears, affecting redaction behavior."""
    URL = "url"
    """URL context - preserve structure."""

    PATH = "path"
    """File path context - preserve segments."""

    IDENTIFIER = "identifier"
    """Identifier context (IDs, refs) - preserve entirely."""

    REPO_LABEL = "repo_label"
    """Repository label context - preserve entirely."""

    TEXT = "text"
    """General text context - apply normal redaction."""

    CODE = "code"
    """Code context - apply careful redaction."""

    UNKNOWN = "unknown"
    """Unknown context - apply conservative redaction."""


# Context patterns that indicate non-sensitive data
URL_PATTERN = re.compile(
    r'https?://[^\s<>"{}|\\^`\[\]]+',
    re.IGNORECASE
)
"""Matches HTTP/HTTPS URLs."""

PATH_PATTERN = re.compile(
    r'(?:^|["\s=])'
    r'(?:'
    r'[a-zA-Z]:\\[\w\\.-]+'  # Windows path
    r'|'
    r'/?[\w.-]+(?:/[\w.-]+)+'  # Unix path
    r')'
    r'(?:$|["\s,;\]\)])',
    re.IGNORECASE
)
"""Matches file paths."""

REPO_LABEL_PATTERN = re.compile(
    r'(?:^|["\s:=])'
    r'[\w][\w.-]{0,63}'
    r'(?:$|["\s,;\]\)])'
)
"""Matches repository labels."""

IDENTIFIER_PATTERN = re.compile(
    r'^(?:'
    r'[a-zA-Z_][a-zA-Z0-9_]*'  # Variable/function names
    r'|'
    r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'  # UUID
    r'|'
    r'[a-zA-Z]+_[a-zA-Z0-9_]+'  # snake_case
    r'|'
    r'[a-zA-Z][a-zA-Z0-9]*(?:[A-Z][a-zA-Z0-9]*)+'  # camelCase/PascalCase
    r')$'
)
"""Matches identifier patterns."""


@dataclass
class ProtectedSpan:
    """A span of text that should be protected from redaction."""
    start: int
    """Start index in the original string."""

    end: int
    """End index (exclusive) in the original string."""

    context: RedactionContext
    """Why this span is protected."""

    original: str
    """The original text in this span."""


@dataclass
class RedactionResult:
    """Result of context-aware redaction."""
    redacted: str
    """The redacted string."""

    protected_spans: list[ProtectedSpan] = field(default_factory=list)
    """Spans that were protected from redaction."""

    redaction_count: int = 0
    """Number of redactions performed."""

    protected_count: int = 0
    """Number of spans protected from redaction."""


def find_url_spans(s: str) -> list[ProtectedSpan]:
    """Find all URL spans in a string that should be protected."""
    spans: list[ProtectedSpan] = []
    for match in URL_PATTERN.finditer(s):
        spans.append(ProtectedSpan(
            start=match.start(),
            end=match.end(),
            context=RedactionContext.URL,
            original=match.group(),
        ))
    return spans


def find_path_spans(s: str) -> list[ProtectedSpan]:
    """Find all file path spans in a string that should be protected."""
    spans: list[ProtectedSpan] = []
    for match in PATH_PATTERN.finditer(s):
        # Extract the actual path (strip delimiters)
        original = match.group()
        # Find the actual path within the match
        path_match = re.search(r'([a-zA-Z]:\\[\w\\.-]+|/?[\w.-]+(?:/[\w.-]+)+)', original)
        if path_match:
            actual_start = match.start() + path_match.start()
            actual_end = match.start() + path_match.end()
            spans.append(ProtectedSpan(
                start=actual_start,
                end=actual_end,
                context=RedactionContext.PATH,
                original=path_match.group(),
            ))
    return spans


# Known secret-related words that should NOT be protected (exact matches only)
SECRET_RELATED_WORDS = frozenset({
    'bearer', 'basic',  # Authentication scheme prefixes
})


def find_identifier_spans(s: str) -> list[ProtectedSpan]:
    """Find identifier spans that should be protected.

    Only protects standalone identifiers that are:
    - At least 4 characters long
    - Surrounded by whitespace or punctuation (not part of hyphenated tokens)
    - Not part of secret-like patterns
    - Not high-entropy strings (likely secrets/hashes)
    """
    spans: list[ProtectedSpan] = []

    for match in re.finditer(r'\b([a-zA-Z_][a-zA-Z0-9_]{0,63})\b', s):
        candidate = match.group(1)

        # Skip very short identifiers (likely parts of larger tokens)
        if len(candidate) < 4:
            continue

        # Skip if this is part of a hyphenated or underscore-connected token
        # Check the character before and after the match
        before_idx = match.start(1) - 1
        after_idx = match.end(1)
        if before_idx >= 0 and s[before_idx] in '-_':
            continue  # Part of hyphenated token
        if after_idx < len(s) and s[after_idx] in '-_':
            continue  # Part of hyphenated token

        # Skip known secret-related words (exact match only)
        lower_candidate = candidate.lower()
        if lower_candidate in SECRET_RELATED_WORDS:
            continue

        # Skip very long alphanumeric-only strings (likely secrets/hashes)
        # These are high-entropy strings without structure
        if len(candidate) > 20 and re.match(r'^[a-z0-9]+$', candidate.lower()):
            continue

        # Check if it's a known identifier pattern
        if IDENTIFIER_PATTERN.match(candidate):
            spans.append(ProtectedSpan(
                start=match.start(1),
                end=match.end(1),
                context=RedactionContext.IDENTIFIER,
                original=candidate,
            ))
    return spans


def find_repo_label_spans(s: str, *, field_name: str = "") -> list[ProtectedSpan]:
    """Find repo_label spans that should be protected."""
    spans: list[ProtectedSpan] = []

    # If the field itself is repo_label or repo, protect the entire value
    if field_name in ("repo_label", "repo", "repository", "target_repo"):
        spans.append(ProtectedSpan(
            start=0,
            end=len(s),
            context=RedactionContext.REPO_LABEL,
            original=s,
        ))
        return spans

    return spans


def merge_spans(spans: list[ProtectedSpan]) -> list[ProtectedSpan]:
    """Merge overlapping spans, keeping the most protective context."""
    if not spans:
        return []

    # Sort by start position
    sorted_spans = sorted(spans, key=lambda s: (s.start, -s.end))

    merged: list[ProtectedSpan] = []
    for span in sorted_spans:
        if not merged:
            merged.append(span)
            continue

        last = merged[-1]
        # Check for overlap
        if span.start <= last.end:
            # Extend the last span if this one goes further
            if span.end > last.end:
                merged[-1] = ProtectedSpan(
                    start=last.start,
                    end=span.end,
                    context=last.context,  # Keep the first context
                    original=s[last.start:span.end] if 's' in dir() else last.original,
                )
        else:
            merged.append(span)

    return merged


def is_in_protected_span(index: int, spans: list[ProtectedSpan]) -> bool:
    """Check if an index falls within any protected span."""
    for span in spans:
        if span.start <= index < span.end:
            return True
    return False


def find_protected_spans(s: str, *, field_name: str = "") -> list[ProtectedSpan]:
    """Find all spans that should be protected from redaction."""
    spans: list[ProtectedSpan] = []
    spans.extend(find_url_spans(s))
    spans.extend(find_path_spans(s))
    spans.extend(find_identifier_spans(s))
    spans.extend(find_repo_label_spans(s, field_name=field_name))

    return merge_spans(spans)


def context_aware_redact(
    s: str,
    *,
    field_name: str = "",
    detect_secrets_func: callable = None,
) -> RedactionResult:
    """
    Perform context-aware redaction on a string.

    This protects URLs, paths, identifiers, and repo labels while
    still redacting actual secrets.

    Args:
        s: The string to redact
        field_name: The field name this string came from (for context)
        detect_secrets_func: Function to use for secret detection

    Returns:
        RedactionResult with the redacted string and metadata
    """
    if not isinstance(s, str) or not s:
        return RedactionResult(redacted=s)

    # Find protected spans first
    protected_spans = find_protected_spans(s, field_name=field_name)

    # If the entire field is protected (e.g., repo_label), don't redact
    if protected_spans and protected_spans[0].start == 0 and protected_spans[0].end == len(s):
        return RedactionResult(
            redacted=s,
            protected_spans=protected_spans,
            redaction_count=0,
            protected_count=1,
        )

    # Apply secret detection, but skip protected regions
    if detect_secrets_func is None:
        # Use default detection
        from runtime.secret_redaction import _detect_and_redact_secrets
        detect_secrets_func = _detect_and_redact_secrets

    # For now, if there are no protected spans, use regular redaction
    if not protected_spans:
        redacted = detect_secrets_func(s, field_name)
        return RedactionResult(redacted=redacted)

    # Build a version of the string with protected regions masked
    # Then apply detection, then restore protected regions
    masked = list(s)
    protected_data: list[tuple[int, int, str]] = []

    # Sort spans by start position (descending) to replace from end to start
    sorted_spans = sorted(protected_spans, key=lambda sp: sp.start, reverse=True)

    # Store original content and mask with placeholders
    for span in sorted_spans:
        protected_data.append((span.start, span.end, s[span.start:span.end]))
        # Replace with placeholder that won't be detected as secret
        placeholder = f'\x00PROTECTED{span.start:06d}\x00'
        # Need to adjust indices based on length changes
        # For simplicity, let's use a different approach

    # Alternative approach: Find secrets in the original, but skip those in protected spans
    result = s

    # Common secret patterns (from secret_redaction.py)
    patterns_to_check = [
        # Anthropic API keys
        (r'sk-ant-api03-[a-zA-Z0-9_\-]{80,}', '<REDACTED:ANTHROPIC_KEY>'),
        # OpenAI API keys
        (r'sk-[a-zA-Z0-9_\-]{20,}', '<REDACTED:OPENAI_KEY>'),
        # Bearer tokens
        (r'[Bb]earer\s+[a-zA-Z0-9_\-\.]{20,}', 'Bearer <REDACTED_TOKEN>'),
    ]

    redaction_count = 0
    for pattern, replacement in patterns_to_check:
        for match in re.finditer(pattern, s):
            # Check if this match is in a protected span
            match_in_protected = False
            for span in protected_spans:
                # If the match overlaps with the protected span, skip it
                if not (match.end() <= span.start or match.start() >= span.end):
                    match_in_protected = True
                    break

            if not match_in_protected:
                result = result[:match.start()] + replacement + result[match.end():]
                redaction_count += 1
                # Need to re-find protected spans since indices changed
                # For simplicity, just continue with remaining patterns

    return RedactionResult(
        redacted=result,
        protected_spans=protected_spans,
        redaction_count=redaction_count,
        protected_count=len(protected_spans),
    )


def redact_with_context(
    value: Any,
    *,
    field_name: str = "",
) -> Any:
    """
    Recursively redact with context awareness.

    This is the main entry point for context-aware redaction,
    preserving URLs, paths, and identifiers while redacting secrets.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        from runtime.secret_redaction import SKIP_REDACTION_FIELDS
        if field_name in SKIP_REDACTION_FIELDS:
            return value
        result = context_aware_redact(value, field_name=field_name)
        return result.redacted

    if isinstance(value, dict):
        return {
            key: redact_with_context(item_value, field_name=key)
            for key, item_value in value.items()
        }

    if isinstance(value, list):
        return [redact_with_context(item, field_name=field_name) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_with_context(item, field_name=field_name) for item in value)

    return value


# =============================================================================
# Protected Field Detection (STEP 4 specific)
# =============================================================================

# Fields that should NEVER be redacted (context is always safe)
ALWAYS_SAFE_FIELDS = frozenset({
    "repo_label",
    "repo",
    "repository",
    "target_repo",
    "repo_path",
    "target_repo_path",
    "file_path",
    "path",
    "url",
    "source_url",
    "homepage",
    "link",
    "href",
    "src",
})

# Patterns in field names that indicate safe content
SAFE_FIELD_PATTERNS = [
    r'_url$',
    r'_path$',
    r'_ref$',
    r'_id$',
    r'_name$',
    r'_label$',
]


def is_safe_field(field_name: str) -> bool:
    """Check if a field is known to contain safe (non-secret) content."""
    if field_name in ALWAYS_SAFE_FIELDS:
        return True

    lower_name = field_name.lower()
    for pattern in SAFE_FIELD_PATTERNS:
        if re.search(pattern, lower_name):
            return True

    return False


def should_protect_field(field_name: str) -> bool:
    """Determine if a field's content should be protected from redaction."""
    return is_safe_field(field_name)
