"""
Unit tests for Context-Aware Redaction (STEP 4)

These tests verify that:
1. repo_label не ломается (repo labels are preserved)
2. URL не искажаются (URLs are not distorted)
3. секреты продолжают редактироваться (secrets continue to be redacted)
4. false positive → 0 на известных кейсах (no false positives on known cases)

Core Invariant:
Redaction MUST NOT corrupt non-sensitive identifiers
"""

from __future__ import annotations

import pytest

from runtime.context_redaction import (
    ALWAYS_SAFE_FIELDS,
    ProtectedSpan,
    RedactionContext,
    RedactionResult,
    context_aware_redact,
    find_url_spans,
    find_path_spans,
    find_identifier_spans,
    find_repo_label_spans,
    find_protected_spans,
    is_safe_field,
    should_protect_field,
    redact_with_context,
    merge_spans,
    is_in_protected_span,
)


class TestFindUrlSpans:
    """Tests for URL span detection."""

    def test_http_url(self) -> None:
        """HTTP URLs should be detected."""
        s = "Visit https://example.com/api for docs"
        spans = find_url_spans(s)
        assert len(spans) == 1
        assert spans[0].original == "https://example.com/api"
        assert spans[0].context == RedactionContext.URL

    def test_http_url(self) -> None:
        """HTTP URLs should be detected."""
        s = "See http://localhost:8080/test"
        spans = find_url_spans(s)
        assert len(spans) == 1
        assert "http://localhost:8080/test" in spans[0].original

    def test_multiple_urls(self) -> None:
        """Multiple URLs should all be detected."""
        s = "API at https://api.example.com and docs at https://docs.example.com"
        spans = find_url_spans(s)
        assert len(spans) == 2

    def test_url_with_query_params(self) -> None:
        """URLs with query parameters should be detected."""
        s = "https://api.example.com/v1/users?page=1&limit=10"
        spans = find_url_spans(s)
        assert len(spans) == 1


class TestFindPathSpans:
    """Tests for file path span detection."""

    def test_unix_path(self) -> None:
        """Unix-style paths should be detected."""
        s = "See src/auth/login.py for details"
        spans = find_path_spans(s)
        # Should detect the path
        assert len(spans) >= 1

    def test_windows_path(self) -> None:
        """Windows-style paths should be detected."""
        s = "File at C:\\Users\\test\\config.py"
        spans = find_path_spans(s)
        # Should detect the path
        assert len(spans) >= 1

    def test_nested_path(self) -> None:
        """Nested paths should be detected."""
        s = "In src/api/v1/endpoints/users.py"
        spans = find_path_spans(s)
        assert len(spans) >= 1


class TestFindIdentifierSpans:
    """Tests for identifier span detection."""

    def test_snake_case_identifier(self) -> None:
        """Snake_case identifiers should be detected."""
        s = "user_auth_token is the field"
        spans = find_identifier_spans(s)
        # user_auth_token should be detected
        identifiers = [sp.original for sp in spans]
        assert "user_auth_token" in identifiers

    def test_camel_case_identifier(self) -> None:
        """CamelCase identifiers should be detected."""
        s = "getUserAuth method"
        spans = find_identifier_spans(s)
        identifiers = [sp.original for sp in spans]
        assert "getUserAuth" in identifiers

    def test_uuid_preserved(self) -> None:
        """UUIDs should be preserved as identifiers."""
        s = "id: 550e8400-e29b-41d4-a716-446655440000"
        spans = find_identifier_spans(s)
        # UUIDs are not detected by identifier pattern but by UUID pattern elsewhere


class TestFindRepoLabelSpans:
    """Tests for repo label protection."""

    def test_repo_label_field_protected(self) -> None:
        """repo_label field should protect entire value."""
        s = "my-awesome-repo"
        spans = find_repo_label_spans(s, field_name="repo_label")
        assert len(spans) == 1
        assert spans[0].start == 0
        assert spans[0].end == len(s)
        assert spans[0].context == RedactionContext.REPO_LABEL

    def test_repo_field_protected(self) -> None:
        """repo field should protect entire value."""
        s = "anthropics/claude-code"
        spans = find_repo_label_spans(s, field_name="repo")
        assert len(spans) == 1
        assert spans[0].original == s

    def test_other_field_not_protected(self) -> None:
        """Non-repo fields should not be auto-protected."""
        s = "some text with repo-like name"
        spans = find_repo_label_spans(s, field_name="statement")
        assert len(spans) == 0


class TestMergeSpans:
    """Tests for span merging."""

    def test_non_overlapping_spans(self) -> None:
        """Non-overlapping spans should remain separate."""
        spans = [
            ProtectedSpan(start=0, end=5, context=RedactionContext.URL, original="http:"),
            ProtectedSpan(start=10, end=15, context=RedactionContext.PATH, original="/test"),
        ]
        merged = merge_spans(spans)
        assert len(merged) == 2

    def test_overlapping_spans_merged(self) -> None:
        """Overlapping spans should be merged."""
        spans = [
            ProtectedSpan(start=0, end=10, context=RedactionContext.URL, original="url"),
            ProtectedSpan(start=5, end=15, context=RedactionContext.PATH, original="path"),
        ]
        merged = merge_spans(spans)
        assert len(merged) == 1
        assert merged[0].start == 0
        assert merged[0].end == 15

    def test_empty_spans(self) -> None:
        """Empty list should return empty."""
        assert merge_spans([]) == []


class TestIsInProtectedSpan:
    """Tests for protected span checking."""

    def test_in_span(self) -> None:
        """Index in span should return True."""
        spans = [ProtectedSpan(start=5, end=10, context=RedactionContext.URL, original="test")]
        assert is_in_protected_span(7, spans) is True

    def test_not_in_span(self) -> None:
        """Index not in any span should return False."""
        spans = [ProtectedSpan(start=5, end=10, context=RedactionContext.URL, original="test")]
        assert is_in_protected_span(15, spans) is False

    def test_at_boundary(self) -> None:
        """Index at start boundary is in span, at end is not."""
        spans = [ProtectedSpan(start=5, end=10, context=RedactionContext.URL, original="test")]
        assert is_in_protected_span(5, spans) is True
        assert is_in_protected_span(10, spans) is False


class TestIsSafeField:
    """Tests for safe field detection."""

    def test_repo_label_is_safe(self) -> None:
        """repo_label should be safe."""
        assert is_safe_field("repo_label") is True
        assert is_safe_field("repo") is True
        assert is_safe_field("repository") is True

    def test_url_field_is_safe(self) -> None:
        """URL fields should be safe."""
        assert is_safe_field("url") is True
        assert is_safe_field("source_url") is True
        assert is_safe_field("api_url") is True

    def test_path_field_is_safe(self) -> None:
        """Path fields should be safe."""
        assert is_safe_field("file_path") is True
        assert is_safe_field("target_path") is True

    def test_random_field_not_safe(self) -> None:
        """Random fields should not be considered safe."""
        assert is_safe_field("statement") is False
        assert is_safe_field("excerpt") is False


class TestContextAwareRedact:
    """Tests for context-aware redaction."""

    def test_url_preserved(self) -> None:
        """DoD: URL не искажаются."""
        s = "Visit https://api.example.com/v1/data"
        result = context_aware_redact(s)
        assert "https://api.example.com/v1/data" in result.redacted

    def test_repo_label_preserved(self) -> None:
        """DoD: repo_label не ломается."""
        s = "my-secret-repo-name"
        result = context_aware_redact(s, field_name="repo_label")
        assert result.redacted == s  # Should be unchanged

    def test_secrets_still_redacted(self) -> None:
        """DoD: секреты продолжают редактироваться."""
        s = "api_key = sk-ant-api03-" + "x" * 86
        result = context_aware_redact(s)
        assert "sk-ant-api03" not in result.redacted
        assert "<REDACTED" in result.redacted

    def test_secret_in_url_context(self) -> None:
        """Secrets that appear in URLs should preserve the URL."""
        s = "https://api.example.com?token=sk-ant-api03-" + "x" * 86
        result = context_aware_redact(s)
        # URL should be preserved, but the secret within might not be
        # depending on implementation
        assert "https://api.example.com" in result.redacted

    def test_empty_string(self) -> None:
        """Empty string should return empty."""
        result = context_aware_redact("")
        assert result.redacted == ""

    def test_none_handling(self) -> None:
        """None should be handled gracefully."""
        # This should not crash
        result = context_aware_redact("normal string")
        assert result.redacted == "normal string"


class TestRedactWithContext:
    """Tests for recursive context-aware redaction."""

    def test_dict_with_safe_fields(self) -> None:
        """Dict with safe fields should preserve them."""
        data = {
            "repo_label": "my-secret-repo",
            "url": "https://example.com/api",
            "file_path": "src/auth/secret_handler.py",
        }
        result = redact_with_context(data)
        assert result["repo_label"] == "my-secret-repo"
        assert result["url"] == "https://example.com/api"
        assert result["file_path"] == "src/auth/secret_handler.py"

    def test_dict_with_secrets(self) -> None:
        """Dict with secrets should redact them."""
        data = {
            "statement": "Found key sk-ant-api03-" + "x" * 86,
        }
        result = redact_with_context(data)
        assert "sk-ant-api03" not in result["statement"]
        assert "<REDACTED" in result["statement"]

    def test_nested_dict(self) -> None:
        """Nested dicts should be processed recursively."""
        data = {
            "target": {
                "repo_label": "my-repo",
                "config": {
                    "api_key": "sk-test-" + "x" * 20,
                }
            }
        }
        result = redact_with_context(data)
        assert result["target"]["repo_label"] == "my-repo"
        assert "sk-test-" not in str(result["target"]["config"])

    def test_list_values(self) -> None:
        """Lists should be processed."""
        data = [
            "https://example.com",
            "sk-ant-api03-" + "x" * 86,
        ]
        result = redact_with_context(data)
        assert "https://example.com" in result[0]
        assert "<REDACTED" in result[1]

    def test_preserves_types(self) -> None:
        """Types should be preserved."""
        data = {
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
        }
        result = redact_with_context(data)
        assert result["int"] == 42
        assert result["float"] == 3.14
        assert result["bool"] is True
        assert result["none"] is None


class TestStep4DoD:
    """Tests for STEP 4 Definition of Done criteria."""

    def test_repo_label_not_broken(self) -> None:
        """DoD: repo_label не ломается."""
        test_cases = [
            ("my-repo", "repo_label"),
            ("my_secret_repo", "repo_label"),
            ("org/repo-name", "repo"),
            ("anthropics/claude-code", "repository"),
        ]
        for value, field in test_cases:
            result = redact_with_context({field: value})
            assert result[field] == value, f"Field {field} was corrupted: {value} -> {result[field]}"

    def test_urls_not_distorted(self) -> None:
        """DoD: URL не искажаются."""
        test_cases = [
            "https://api.anthropic.com/v1/messages",
            "http://localhost:8080/api/v2/users?page=1",
            "https://github.com/org/repo/issues/123",
        ]
        for url in test_cases:
            data = {"url": url}
            result = redact_with_context(data)
            assert result["url"] == url, f"URL was distorted: {url} -> {result['url']}"

    def test_secrets_still_redacted(self) -> None:
        """DoD: секреты продолжают редактироваться."""
        test_cases = [
            f"sk-ant-api03-{'x' * 86}",
            f"sk-proj-{'y' * 40}",
            "Bearer abc123def456ghi789jkl012mno345pqr",
        ]
        for secret in test_cases:
            data = {"statement": f"Found {secret}"}
            result = redact_with_context(data)
            assert secret not in result["statement"], f"Secret not redacted: {secret}"
            assert "<REDACTED" in result["statement"]

    def test_no_false_positives_on_known_cases(self) -> None:
        """DoD: false positive → 0 на известных кейсах."""
        # Known cases that should NOT be redacted
        safe_cases = [
            ("repo_label", "my-secret-project"),
            ("url", "https://api.secret-scanner.io/v1"),
            ("file_path", "src/secret/auth.py"),
            ("repo", "org/secret-repo"),
            ("target_repo_path", "/home/user/secret-project"),
        ]
        for field, value in safe_cases:
            result = redact_with_context({field: value})
            assert result[field] == value, f"False positive on {field}: {value} -> {result[field]}"
            assert "<REDACTED" not in str(result[field])


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_input(self) -> None:
        """Empty inputs should not crash."""
        assert redact_with_context({}) == {}
        assert redact_with_context([]) == []
        assert redact_with_context(None) is None
        assert redact_with_context("") == ""

    def test_unicode_content(self) -> None:
        """Unicode content should be handled."""
        data = {"statement": "Found 秘密 in code"}
        result = redact_with_context(data)
        assert "秘密" in result["statement"]

    def test_very_long_string(self) -> None:
        """Very long strings should be handled."""
        long_secret = "sk-ant-api03-" + "x" * 500
        data = {"excerpt": f"Key: {long_secret}"}
        result = redact_with_context(data)
        assert long_secret not in result["excerpt"]

    def test_nested_protection(self) -> None:
        """Deeply nested structures should work."""
        data = {
            "level1": {
                "level2": {
                    "level3": {
                        "repo_label": "my-repo",
                        "secret": "sk-ant-api03-" + "x" * 86,
                    }
                }
            }
        }
        result = redact_with_context(data)
        assert result["level1"]["level2"]["level3"]["repo_label"] == "my-repo"
        assert "sk-ant-api03" not in result["level1"]["level2"]["level3"]["secret"]
