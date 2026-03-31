"""Deterministic pre-scan for known vulnerability patterns.

This module implements a regex-based pattern scanner that runs BEFORE the LLM
Reader sees the code. It produces concrete signal matches that get injected into
the worker input as ``pattern_matches``. The Reader then validates these signals
against actual code context and emits observations.

This is the "deterministic layer" of the hybrid approach:
  deterministic pattern detection -> LLM reasoning/validation

Pattern categories (L2):
  - SQL injection: string concatenation in SQL queries
  - Weak crypto: md5, sha1 used for security purposes
  - Dangerous deserialization: pickle.loads, yaml.load without SafeLoader
  - Code execution: eval(), exec() on dynamic input
  - Secret exposure: hardcoded passwords, API keys, default secrets
  - Unsigned tokens: JWT without verification, base64-only "encoding"
  - Input trust: user input used without validation
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PatternMatch:
    """A single pattern match from the deterministic pre-scan."""

    pattern_match_id: str  # stable deterministic ID
    rule_id: str
    category: str
    file_path: str
    line_start: int
    line_end: int
    matched_text: str
    confidence: str  # "high", "medium", "low"
    description: str
    severity_hint: str  # "critical", "high", "medium", "low", "info"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_match_id": self.pattern_match_id,
            "rule_id": self.rule_id,
            "category": self.category,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "matched_text": self.matched_text,
            "confidence": self.confidence,
            "description": self.description,
            "severity_hint": self.severity_hint,
        }


def compute_pattern_match_id(
    snapshot_ref: str,
    file_path: str,
    line_start: int,
    line_end: int,
    rule_id: str,
    matched_text: str,
) -> str:
    """Deterministic pattern_match_id from stable fields."""
    payload = f"{snapshot_ref}\n{file_path}\n{line_start}\n{line_end}\n{rule_id}\n{matched_text}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"pm_{rule_id}_{digest}"


# ---------------------------------------------------------------------------
# Pattern definitions — (rule_id, compiled_regex, category, description, severity)
# ---------------------------------------------------------------------------

PATTERNS = [
    # --- SQL Injection ---
    (
        "sql_string_concat",
        re.compile(r'(?i)(?:execute|cursor\.execute)\s*\(.*?\+'),
        "sql_injection",
        "SQL query built with string concatenation",
        "critical",
    ),
    (
        "sql_fstring",
        re.compile(r'(?i)(?:SELECT|INSERT|UPDATE|DELETE|DROP)\s.*\{.*\}'),
        "sql_injection",
        "SQL query uses f-string interpolation",
        "critical",
    ),
    (
        "sql_format_string",
        re.compile(r'(?i)(?:execute|cursor\.execute)\s*\(.*?\.format\s*\('),
        "sql_injection",
        "SQL query uses string .format()",
        "critical",
    ),

    # --- Weak Cryptography ---
    (
        "weak_hash_md5",
        re.compile(r'(?i)(?:hashlib\.)?md5\s*\('),
        "weak_crypto",
        "MD5 hash function used - cryptographically broken",
        "high",
    ),
    (
        "weak_hash_sha1",
        re.compile(r'(?i)(?:hashlib\.)?sha1\s*\('),
        "weak_crypto",
        "SHA-1 hash function used - cryptographically weak",
        "high",
    ),
    (
        "weak_random",
        re.compile(r'(?i)\brandom\.(?:random|randint|choice|randrange|shuffle)\s*\('),
        "weak_crypto",
        "Insecure random module used - not suitable for security",
        "medium",
    ),

    # --- Dangerous Deserialization ---
    (
        "pickle_loads",
        re.compile(r'pickle\.loads?\s*\('),
        "dangerous_deserialization",
        "pickle.loads on potentially untrusted data - arbitrary code execution",
        "critical",
    ),
    (
        "yaml_unsafe_load",
        re.compile(r'yaml\.load\s*\('),
        "dangerous_deserialization",
        "yaml.load without explicit SafeLoader - potential RCE",
        "critical",
    ),
    (
        "marshal_loads",
        re.compile(r'marshal\.loads?\s*\('),
        "dangerous_deserialization",
        "marshal.loads on potentially untrusted data",
        "high",
    ),

    # --- Code Execution ---
    (
        "eval_usage",
        re.compile(r'\beval\s*\('),
        "code_execution",
        "eval() used - potential code injection if input is not sanitized",
        "critical",
    ),
    (
        "exec_usage",
        re.compile(r'\bexec\s*\('),
        "code_execution",
        "exec() used - potential code injection if input is not sanitized",
        "critical",
    ),
    (
        "subprocess_shell",
        re.compile(r'subprocess\.\w+\s*\([^)]*shell\s*=\s*True'),
        "code_execution",
        "subprocess with shell=True - potential command injection",
        "high",
    ),

    # --- Secret Exposure ---
    (
        "hardcoded_password",
        re.compile(r'(?i)(?:password|passwd|pwd|secret)\s*=\s*["\'][^"\']{3,}["\']'),
        "secret_exposure",
        "Hardcoded password or secret value",
        "critical",
    ),
    (
        "default_secret_key",
        re.compile(r'(?i)SECRET_KEY\s*=\s*["\'][^"\']+["\']'),
        "secret_exposure",
        "Hardcoded SECRET_KEY - should come from environment",
        "high",
    ),
    (
        "api_key_in_source",
        re.compile(r'(?i)(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token)\s*=\s*["\'][a-zA-Z0-9_\-]{16,}["\']'),
        "secret_exposure",
        "Hardcoded API key or access token",
        "critical",
    ),

    # --- Unsigned/Unverified Tokens ---
    (
        "jwt_no_verify",
        re.compile(r'jwt\.(?:decode|encode)\s*\([^)]*verify[^:]*:\s*False'),
        "unsigned_token",
        "JWT decoded without signature verification",
        "critical",
    ),
    (
        "base64_as_security",
        re.compile(r'(?i)base64\.(?:b64decode|b64encode)\s*\('),
        "unsigned_token",
        "Base64 encoding/decoding used - encoding is not encryption",
        "high",
    ),

    # --- Input Trust / Configuration ---
    (
        "flask_debug",
        re.compile(r'app\.run\s*\([^)]*debug\s*=\s*True'),
        "input_trust",
        "Flask debug mode enabled in application runner",
        "high",
    ),
    (
        "django_debug",
        re.compile(r'DEBUG\s*=\s*True'),
        "input_trust",
        "Django DEBUG mode enabled",
        "medium",
    ),
    (
        "cors_wildcard",
        re.compile(r'(?i)CORS.*?\*'),
        "input_trust",
        "CORS configured to allow all origins",
        "medium",
    ),
    (
        "disabled_csrf",
        re.compile(r'@csrf_exempt|csrf_protect\s*=\s*False|CSRF\s*=\s*False'),
        "input_trust",
        "CSRF protection explicitly disabled",
        "high",
    ),
    (
        "sqlalchemy_text_concat",
        re.compile(r'\.execute\s*\(\s*text\s*\(\s*["\'].*?\+'),
        "sql_injection",
        "SQLAlchemy text() with string concatenation",
        "high",
    ),
    (
        "assert_in_production",
        re.compile(r'\bassert\s+'),
        "input_trust",
        "assert statement used - stripped in optimized mode (-O)",
        "low",
    ),
]


def scan_file(
    file_path: str,
    file_content: str,
    *,
    snapshot_ref: str = "",
    max_matches_per_rule: int = 5,
    max_total_matches: int = 50,
) -> list[PatternMatch]:
    """Scan a single file's content against all pattern rules."""
    if not file_content or not file_path:
        return []

    lines = file_content.splitlines()
    norm_path = file_path.replace("\\", "/")
    matches: list[PatternMatch] = []

    for rule_id, pattern, category, description, severity_hint in PATTERNS:
        rule_count = 0
        for m in pattern.finditer(file_content):
            if rule_count >= max_matches_per_rule:
                break
            if len(matches) >= max_total_matches:
                return sorted(matches, key=lambda x: (x.line_start, x.rule_id))

            line_start = _line_from_pos(file_content, m.start())
            line_end = _line_from_pos(file_content, m.end() - 1) if m.end() > m.start() else line_start
            line_start = min(line_start, len(lines))
            line_end = min(line_end, len(lines))

            matched_text = m.group(0)
            if len(matched_text) > 200:
                matched_text = matched_text[:197] + "..."

            pm_id = compute_pattern_match_id(
                snapshot_ref=snapshot_ref,
                file_path=norm_path,
                line_start=line_start,
                line_end=line_end,
                rule_id=rule_id,
                matched_text=matched_text,
            )

            matches.append(
                PatternMatch(
                    pattern_match_id=pm_id,
                    rule_id=rule_id,
                    category=category,
                    file_path=norm_path,
                    line_start=line_start,
                    line_end=line_end,
                    matched_text=matched_text,
                    confidence=_classify_confidence(rule_id),
                    description=description,
                    severity_hint=severity_hint,
                )
            )
            rule_count += 1

    return sorted(matches, key=lambda x: (x.line_start, x.rule_id))


def scan_target_sources(
    target_sources: list[dict[str, Any]],
    *,
    snapshot_ref: str = "",
) -> list[PatternMatch]:
    """Scan all target_sources entries for pattern matches."""
    all_matches: list[PatternMatch] = []
    for source in target_sources:
        file_path = source.get("file_path", "")
        file_content = source.get("file_content", "")
        if not isinstance(file_path, str) or not isinstance(file_content, str):
            continue
        matches = scan_file(file_path, file_content, snapshot_ref=snapshot_ref)
        all_matches.extend(matches)
    return all_matches


def _line_from_pos(text: str, pos: int) -> int:
    """Return 1-based line number for a character position."""
    return text[:max(pos, 0)].count("\n") + 1


def _classify_confidence(rule_id: str) -> str:
    """Classify confidence level based on rule specificity."""
    high_confidence_rules = {
        "pickle_loads",
        "eval_usage",
        "exec_usage",
        "hardcoded_password",
        "api_key_in_source",
        "jwt_no_verify",
        "disabled_csrf",
        "flask_debug",
        "sql_fstring",
    }
    if rule_id in high_confidence_rules:
        return "high"
    return "medium"
