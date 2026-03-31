"""
Tests for Semantic Content Validator (STEP 0.1)

Tests that the validator catches "garbage as truth" - well-formed events
with placeholder/empty semantic content.

IMPORTANT: The validator operates in ADVISORY mode by default, meaning
it returns empty list (accepts all events) but logs quality issues.
Tests verify internal detection logic, not rejection behavior.
"""

import pytest

from runtime.validators.semantic_content import SemanticContentValidator
from runtime.validators.models import ValidationIssue


@pytest.fixture
def validator():
    return SemanticContentValidator("/tmp/test_workspace")


class TestObservationSemanticContent:
    """Tests for observation semantic content validation."""

    def test_valid_observation_passes(self, validator):
        """Observation with real content passes validation."""
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "The CircleCI config uses Python 3.6.0",
                "provenance": {
                    "source_refs": [{
                        "file_path": ".circleci/config.yml",
                        "line_range": {"start": 5, "end": 7},
                    }]
                }
            }
        }
        issues = validator.validate(event)
        assert issues == []

    def test_empty_statement_detected_in_advisory_mode(self, validator):
        """In advisory mode, empty statement is detected but not rejected."""
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "",
                "provenance": {
                    "source_refs": [{
                        "file_path": ".circleci/config.yml",
                        "line_range": {"start": 5, "end": 7},
                    }]
                }
            }
        }
        # In advisory mode, returns empty (events pass through)
        issues = validator.validate(event)
        assert issues == []  # Advisory mode - accepts all
        # But internal method detects the issue
        internal_issues = validator._validate_observation_content(event, event["payload"])
        assert len(internal_issues) == 1
        assert internal_issues[0].code == "OBS_EMPTY_STATEMENT"

    def test_placeholder_statement_detected(self, validator):
        """Placeholder statement is detected internally."""
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "Untitled observation",
                "provenance": {
                    "source_refs": [{
                        "file_path": ".circleci/config.yml",
                        "line_range": {"start": 5, "end": 7},
                    }]
                }
            }
        }
        issues = validator.validate(event)
        assert issues == []  # Advisory mode
        internal_issues = validator._validate_observation_content(event, event["payload"])
        assert len(internal_issues) == 1
        assert internal_issues[0].code == "OBS_EMPTY_STATEMENT"

    def test_unknown_file_path_detected(self, validator):
        """Unknown file_path is detected internally."""
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "Some statement",
                "provenance": {
                    "source_refs": [{
                        "file_path": "unknown",
                        "line_range": {"start": 5, "end": 7},
                    }]
                }
            }
        }
        issues = validator.validate(event)
        assert issues == []  # Advisory mode
        internal_issues = validator._validate_observation_content(event, event["payload"])
        assert len(internal_issues) == 1
        assert internal_issues[0].code == "OBS_NO_SOURCE"

    def test_fake_binding_detected(self, validator):
        """Fake binding (unknown + line 1,1) is detected internally."""
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "Some statement",
                "provenance": {
                    "source_refs": [{
                        "file_path": "unknown",
                        "line_range": {"start": 1, "end": 1},
                    }]
                }
            }
        }
        issues = validator.validate(event)
        assert issues == []  # Advisory mode
        internal_issues = validator._validate_observation_content(event, event["payload"])
        # Should get OBS_NO_SOURCE and OBS_FAKE_BINDING, aggregated to OBS_EMPTY_SEMANTIC
        assert len(internal_issues) >= 1
        codes = [i.code for i in internal_issues]
        assert "OBS_EMPTY_SEMANTIC" in codes or "OBS_NO_SOURCE" in codes or "OBS_FAKE_BINDING" in codes

    def test_multiple_issues_aggregated(self, validator):
        """Multiple issues are aggregated into OBS_EMPTY_SEMANTIC."""
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "Untitled observation",  # Issue 1
                "provenance": {
                    "source_refs": [{
                        "file_path": "unknown",  # Issue 2
                        "line_range": {"start": 1, "end": 1},  # Issue 3
                    }]
                }
            }
        }
        issues = validator.validate(event)
        assert issues == []  # Advisory mode
        internal_issues = validator._validate_observation_content(event, event["payload"])
        assert len(internal_issues) == 1
        assert internal_issues[0].code == "OBS_EMPTY_SEMANTIC"

    def test_no_source_refs_detected(self, validator):
        """Observation with no source_refs is detected internally."""
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "Some statement",
                "provenance": {
                    "source_refs": []
                }
            }
        }
        issues = validator.validate(event)
        assert issues == []  # Advisory mode
        internal_issues = validator._validate_observation_content(event, event["payload"])
        assert len(internal_issues) == 1
        assert internal_issues[0].code == "OBS_NO_SOURCE_REFS"


class TestHypothesisSemanticContent:
    """Tests for hypothesis semantic content validation."""

    def test_valid_hypothesis_passes(self, validator):
        """Hypothesis with real content passes validation."""
        event = {
            "event_type": "hypothesis.proposed",
            "entity_type": "hypothesis",
            "payload": {
                "statement": "The app may be vulnerable to injection",
                "rationale": "Based on evidence in config.py",
            }
        }
        issues = validator.validate(event)
        assert issues == []

    def test_empty_hypothesis_statement_detected(self, validator):
        """Empty hypothesis statement is detected internally."""
        event = {
            "event_type": "hypothesis.proposed",
            "entity_type": "hypothesis",
            "payload": {
                "statement": "",
                "rationale": "Some rationale",
            }
        }
        issues = validator.validate(event)
        assert issues == []  # Advisory mode
        internal_issues = validator._validate_hypothesis_content(event, event["payload"])
        assert len(internal_issues) == 1
        assert internal_issues[0].code == "HYP_EMPTY_STATEMENT"

    def test_empty_hypothesis_rationale_detected(self, validator):
        """Placeholder rationale is detected internally."""
        event = {
            "event_type": "hypothesis.proposed",
            "entity_type": "hypothesis",
            "payload": {
                "statement": "Some statement",
                "rationale": "No rationale provided",
            }
        }
        issues = validator.validate(event)
        assert issues == []  # Advisory mode
        internal_issues = validator._validate_hypothesis_content(event, event["payload"])
        assert len(internal_issues) == 1
        assert internal_issues[0].code == "HYP_EMPTY_RATIONALE"


class TestOtherEvents:
    """Tests for non-observation/hypothesis events."""

    def test_audit_event_ignored(self, validator):
        """Audit events are not validated for semantic content."""
        event = {
            "event_type": "audit.created",
            "entity_type": "audit",
            "payload": {"id": "audit_001"}
        }
        issues = validator.validate(event)
        assert issues == []

    def test_task_event_ignored(self, validator):
        """Task events are not validated for semantic content."""
        event = {
            "event_type": "task.completed",
            "entity_type": "task",
            "payload": {"id": "task_001"}
        }
        issues = validator.validate(event)
        assert issues == []

    def test_advisory_mode_configurable(self, validator):
        """Advisory mode can be toggled to strict mode."""
        # Default is advisory
        assert validator.ADVISORY_MODE is True

        # In strict mode, issues would be returned
        # (but we don't change the default for safety)
        event = {
            "event_type": "observation.verified",
            "entity_type": "observation",
            "payload": {
                "statement": "Untitled observation",
                "provenance": {
                    "source_refs": [{
                        "file_path": "unknown",
                        "line_range": {"start": 1, "end": 1},
                    }]
                }
            }
        }

        # In advisory mode: returns empty (event accepted)
        issues = validator.validate(event)
        assert issues == []

        # Internal detection still works
        internal_issues = validator._validate_observation_content(event, event["payload"])
        assert len(internal_issues) == 1
