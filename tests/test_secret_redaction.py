"""Tests for secret redaction in audit artifacts.

These tests verify that secrets are properly redacted before persistence.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from runtime.secret_redaction import (
    MIN_SECRET_LENGTH,
    MIN_ENTROPY_RATIO,
    redact_string,
    redact_value,
    redact_event,
    redact_canonical_state,
    redact_report,
    redact_slice,
    redact_trace_entry,
    _detect_and_redact_secrets,
    _calculate_entropy_ratio,
    _looks_like_uuid,
    _looks_like_base64,
    _has_secret_context,
    should_redact_field,
    SKIP_REDACTION_FIELDS,
    TEXT_CONTENT_FIELDS,
)


class TestSecretDetection:
    """Test secret detection patterns."""

    def test_anthropic_key_detection(self):
        """Anthropic-like API keys should be detected."""
        test_cases = [
            'sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            'sk-ant-api03-8f1b3f4aacb84ddc8b9f47a8b2d6d37412345678901234567890123456789012345678901234567890',
        ]
        for case in test_cases:
            result = _detect_and_redact_secrets(case, "test")
            assert "sk-ant-api03" not in result, f"Anthropic key not redacted: {case}"
            assert "<REDACTED" in result

    def test_openai_key_detection(self):
        """OpenAI-like API keys should be detected."""
        test_cases = [
            'sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            'sk-proj-abc123def456ghi789jkl012mno345pqr678',
        ]
        for case in test_cases:
            result = _detect_and_redact_secrets(case, "test")
            assert "sk-proj-" not in result, f"OpenAI key not redacted: {case}"
            assert "<REDACTED" in result

    def test_bearer_token_detection(self):
        """Bearer tokens should be detected."""
        test_cases = [
            'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefghij',
            'Authorization: Bearer abc123def456ghi789jkl012mno345pqr',
            'bearer abc123def456ghi789jkl012mno345pqr678',
        ]
        for case in test_cases:
            result = _detect_and_redact_secrets(case, "test")
            # Check that the token part is redacted
            assert "<REDACTED" in result, f"Bearer token not redacted: {case}"

    def test_password_assignment_detection(self):
        """Password/secret assignments should be detected."""
        test_cases = [
            'password = "super_secret_password_12345678"',
            'secret = "my_api_key_here_12345678"',
            'api_key = "8f1b3f4aacb84ddc8b9f47a8b2d6d374"',
            'token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"',
        ]
        for case in test_cases:
            result = _detect_and_redact_secrets(case, "test")
            assert "<REDACTED" in result, f"Secret assignment not redacted: {case}"

    def test_high_entropy_detection(self):
        """High entropy strings in secret context should be detected."""
        high_entropy = "xK9mN2pL5qR8sT1uV4wX7yZ0aB3cD6eF9gH2iJ5k"
        # Must be in secret context
        context = f"api_key = {high_entropy}"
        result = _detect_and_redact_secrets(context, "test")
        assert "<REDACTED" in result

    def test_uuid_not_redacted_normally(self):
        """UUIDs should not be redacted outside secret context."""
        uuid_like = "550e8400-e29b-41d4-a716-446655440000"
        result = _detect_and_redact_secrets(uuid_like, "observation_id")
        # UUID should not match patterns
        assert result == uuid_like

    def test_no_false_positives_on_ordinary_strings(self):
        """Ordinary code strings should not be redacted."""
        safe_strings = [
            "token_bucket_capacity = 64",
            "max_connections = 100",
            "timeout_seconds = 300",
            "def calculate_total(items):",
            "return sum(item.value for item in items)",
            "The user requested access to the system.",
            "This function validates the input parameters.",
            "configuration = load_config()",
        ]
        for s in safe_strings:
            result = redact_string(s, "test")
            assert result == s, f"Should not redact: {s}"

    def test_short_strings_not_redacted(self):
        """Short strings should not be redacted."""
        short_strings = [
            "abc",
            "test",
            "key",
            "pass",
        ]
        for s in short_strings:
            result = redact_string(s, "test")
            assert result == s


class TestRedactionFunctions:
    """Test redaction functions."""

    def test_redact_string_basic(self):
        """Basic string redaction should work."""
        original = 'api_key = "sk-ant-api03-test123456789012345678901234567890123456789012345678901234567890"'
        redacted = redact_string(original, "excerpt")
        assert "sk-ant-api03-test123456789012345678901234567890123456789012345678901234567890" not in redacted
        assert "<REDACTED" in redacted

    def test_redact_string_preserves_structure(self):
        """Redaction should preserve string structure."""
        original = 'password = "supersecret12345678"'
        redacted = redact_string(original, "excerpt")
        assert 'password' in redacted.lower()
        assert "<REDACTED" in redacted

    def test_redact_value_dict(self):
        """Dict values should be redacted recursively."""
        data = {
            "statement": "The API key is sk-proj-test123456789012345678901234",
            "safe_field": "This is safe",
            "nested": {
                "excerpt": "Bearer abc123def456ghi789jkl012mno345pqr",
            }
        }
        redacted = redact_value(data)
        redacted_str = str(redacted)
        assert "sk-proj-test123456789012345678901234" not in redacted_str
        assert "abc123def456ghi789jkl012mno345pqr" not in redacted_str
        assert redacted["safe_field"] == "This is safe"

    def test_redact_value_list(self):
        """List values should be redacted recursively."""
        data = [
            "Bearer abc123def456ghi789jkl012mno345pqr678",
            "safe_item",
        ]
        redacted = redact_value(data)
        redacted_str = str(redacted)
        assert "abc123def456ghi789jkl012mno345pqr678" not in redacted_str
        assert "safe_item" in redacted

    def test_redact_value_preserves_types(self):
        """Redaction should preserve types."""
        data = {
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "string": "safe",
        }
        redacted = redact_value(data)
        assert redacted["int"] == 42
        assert redacted["float"] == 3.14
        assert redacted["bool"] is True
        assert redacted["none"] is None
        assert redacted["string"] == "safe"

    def test_redact_value_skip_fields(self):
        """IDs and timestamps should not be redacted."""
        data = {
            "id": "obs_abc123def456ghi789",
            "audit_id": "audit_xyz789abc123def",
            "created_at": "2024-01-01T00:00:00Z",
            "statement": "The key is xK9mN2pL5qR8sT1uV4wX7yZ0",
        }
        redacted = redact_value(data)
        assert redacted["id"] == "obs_abc123def456ghi789"
        assert redacted["audit_id"] == "audit_xyz789abc123def"
        assert redacted["created_at"] == "2024-01-01T00:00:00Z"


class TestEventRedaction:
    """Test event redaction."""

    def test_redact_event_provenance_excerpt(self):
        """Secrets in provenance excerpt should be redacted."""
        event = {
            "id": "event_test123",
            "audit_id": "audit_001",
            "entity_type": "observation",
            "event_type": "observation.proposed",
            "payload": {
                "id": "obs_001",
                "statement": "Found hardcoded key",
                "provenance": {
                    "source_refs": [
                        {
                            "file_path": "src/config.py",
                            "line_range": {"start": 10, "end": 12},
                            "excerpt": 'api_key = "sk-ant-api03-test123456789012345678901234567890123456789012345678901234567890"',
                        }
                    ]
                }
            }
        }
        redacted = redact_event(event)
        redacted_str = str(redacted)
        assert "sk-ant-api03-test123456789012345678901234567890123456789012345678901234567890" not in redacted_str
        assert "<REDACTED" in redacted_str

        # Structure preserved
        assert redacted["id"] == "event_test123"
        assert redacted["payload"]["provenance"]["source_refs"][0]["file_path"] == "src/config.py"
        assert redacted["payload"]["provenance"]["source_refs"][0]["line_range"]["start"] == 10

    def test_redact_event_statement(self):
        """Secrets in statement should be redacted."""
        event = {
            "id": "event_test123",
            "audit_id": "audit_001",
            "entity_type": "observation",
            "event_type": "observation.proposed",
            "payload": {
                "id": "obs_001",
                "statement": "Found Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefgh in code",
            }
        }
        redacted = redact_event(event)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefgh" not in redacted["payload"]["statement"]
        assert "<REDACTED" in redacted["payload"]["statement"]

    def test_redact_issue_summary(self):
        """Secrets in issue summary should be redacted."""
        event = {
            "id": "event_issue001",
            "audit_id": "audit_001",
            "entity_type": "issue",
            "event_type": "issue.proposed",
            "payload": {
                "id": "issue_001",
                "title": "Hardcoded credential",
                "summary": "The password abc123def456ghi789jkl is hardcoded in auth.py",
            }
        }
        redacted = redact_event(event)
        assert "abc123def456ghi789jkl" not in redacted["payload"]["summary"]


class TestDeterministicRedaction:
    """Test deterministic behavior of redaction."""

    def test_same_secret_same_redaction(self):
        """Same secret should be redacted to same representation."""
        secret = "sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        result1 = redact_string(secret, "test")
        result2 = redact_string(secret, "test")
        assert result1 == result2


class TestCanonicalStateRedaction:
    """Test canonical state redaction."""

    def test_redact_canonical_state(self):
        """Canonical state should have secrets redacted."""
        state = {
            "schema_version": "1.0.0",
            "audit": {
                "id": "audit_001",
            },
            "observations": {
                "obs_001": {
                    "id": "obs_001",
                    "statement": "Found key sk-proj-test123456789012345678901234",
                    "provenance": {
                        "source_refs": [
                            {
                                "file_path": "src/config.py",
                                "line_range": {"start": 1, "end": 2},
                                "excerpt": 'SECRET = "abc123def456ghi789jkl012mno"',
                            }
                        ]
                    }
                }
            }
        }
        redacted = redact_canonical_state(state)
        redacted_str = str(redacted)
        assert "sk-proj-test123456789012345678901234" not in redacted_str
        assert "abc123def456ghi789jkl012mno" not in redacted_str
        # Structure preserved
        assert redacted["schema_version"] == "1.0.0"
        assert "obs_001" in redacted["observations"]


class TestReportRedaction:
    """Test report redaction."""

    def test_redact_report(self):
        """Reports should have secrets redacted."""
        report = {
            "schema_version": "1.0.0",
            "report_id": "report_abc123",
            "source_audit_id": "audit_001",
            "findings": [
                {
                    "issue_id": "issue_001",
                    "title": "Hardcoded API Key",
                    "summary": "Found Bearer abc123def456ghi789jkl012mno345pqr in config",
                }
            ]
        }
        redacted = redact_report(report)
        redacted_str = str(redacted)
        assert "abc123def456ghi789jkl012mno345pqr" not in redacted_str
        assert redacted["report_id"] == "report_abc123"


class TestSliceRedaction:
    """Test slice redaction."""

    def test_redact_slice(self):
        """Slices should have secrets redacted."""
        slice_payload = {
            "schema_version": "1.0.0",
            "slice_id": "slice_abc123",
            "worker_role": "Reader",
            "relevant_observations": {
                "obs_001": {
                    "id": "obs_001",
                    "statement": "Found credential abc123def456ghi789jkl012mno",
                    "provenance": {
                        "source_refs": [
                            {
                                "file_path": "src/auth.py",
                                "line_range": {"start": 10, "end": 12},
                                "excerpt": 'password = "superSecretPassword123456789"',
                            }
                        ]
                    }
                }
            }
        }
        redacted = redact_slice(slice_payload)
        redacted_str = str(redacted)
        assert "superSecretPassword123456789" not in redacted_str
        assert redacted["slice_id"] == "slice_abc123"


class TestTraceEntryRedaction:
    """Test trace entry redaction."""

    def test_redact_trace_entry(self):
        """Trace entries should have secrets redacted."""
        entry = {
            "schema_version": "1.0.0",
            "entry_type": "worker_execution",
            "entry_id": "trace_00000001",
            "run_id": "run_0001",
            "error_message": "Failed with token Bearer abc123def456ghi789jkl012mno345pqr",
        }
        redacted = redact_trace_entry(entry)
        redacted_str = str(redacted)
        assert "abc123def456ghi789jkl012mno345pqr" not in redacted_str
        assert redacted["entry_id"] == "trace_00000001"


class TestFieldFiltering:
    """Test field filtering logic."""

    def test_skip_redaction_fields(self):
        """Skip fields should not be redacted."""
        for field in SKIP_REDACTION_FIELDS:
            assert should_redact_field(field) is False, f"{field} should be skipped"

    def test_text_content_fields(self):
        """Text content fields should be redacted."""
        for field in TEXT_CONTENT_FIELDS:
            assert should_redact_field(field) is True, f"{field} should be redacted"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
