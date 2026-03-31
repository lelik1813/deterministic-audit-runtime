"""Tests for rejection classification in _trace_rejection / _trace_outcome. """
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.rejection import RejectionReason, RejectionStage
from runtime.processing import (
    _VALIDATOR_CODE_TO_REJECTION,
    _FALLBACK,
    _trace_rejection,
    _trace_outcome,
)
from runtime.validators.models import ValidationIssue


class TestRejectionClassification:

    def test_all_known_codes_mapped(self):
        for code in _VALIDATOR_CODE_TO_REJECTION:
            reason, stage = _VALIDATOR_CODE_TO_REJECTION[code]
            assert isinstance(reason, RejectionReason)
            assert isinstance(stage, RejectionStage)

    def test_fallback_values(self):
        assert _FALLBACK == (RejectionReason.SCHEMA_INVALID, RejectionStage.SCHEMA)

    def test_unknown_code_falls_back(self):
        reason, stage = _VALIDATOR_CODE_TO_REJECTION.get("totally_unknown_xyz", _FALLBACK)
        assert reason == RejectionReason.SCHEMA_INVALID
        assert stage == RejectionStage.SCHEMA

    def test_trace_rejection_with_issues(self):
        issues = (
            ValidationIssue(
                validator="schema",
                code="schema_validation_failed",
                message="Required field missing",
            ),
            ValidationIssue(
                validator="source_binding",
                code="missing_source_refs",
                message="No source refs",
            ),
        )
        detail = _trace_rejection(issues)
        assert detail is not None
        assert detail["rejection_code"] == "schema_invalid"
        assert detail["rejection_layer"] == "schema"
        assert detail["rejection_message"] == "Required field missing"
        assert detail["validator"] == "schema"
        assert detail["validator_code"] == "schema_validation_failed"
        assert len(detail["all_issue_codes"]) == 2

    def test_trace_rejection_no_issues(self):
        detail = _trace_rejection(())
        assert detail is None

    def test_trace_outcome_rejected(self):
        outcome = MagicMock()
        outcome.outcome = "rejected"
        outcome.issues = (
            ValidationIssue(validator="schema", code="schema_validation_failed", message="schema fail"),
        )
        outcome.event_id = "evt_001"
        outcome.event_type = "observation.proposed"
        outcome.entity_type = "observation"
        outcome.entity_id = "obs_001"
        outcome.append_result = None

        trace = _trace_outcome(outcome)
        assert trace["rejection"] is not None
        assert trace["rejection"]["rejection_code"] == "schema_invalid"
        assert trace["rejection"]["rejection_layer"] == "schema"
        assert trace["outcome"] == "rejected"

    def test_trace_outcome_accepted(self):
        mock_result = MagicMock()
        mock_result.ledger_line_number = 1
        mock_result.ledger_path = Path("/fake/ledger.json")

        outcome = MagicMock()
        outcome.outcome = "accepted"
        outcome.issues = ()
        outcome.event_id = "evt_002"
        outcome.event_type = "observation.proposed"
        outcome.entity_type = "observation"
        outcome.entity_id = "obs_002"
        outcome.append_result = mock_result

        trace = _trace_outcome(outcome)
        assert trace["rejection"] is None
        assert trace["outcome"] == "accepted"

    def test_trace_outcome_multiple_issues(self):
        outcome = MagicMock()
        outcome.outcome = "rejected"
        outcome.issues = (
            ValidationIssue(validator="schema", code="schema_validation_failed", message="schema fail"),
            ValidationIssue(validator="source_binding", code="missing_source_refs", message="no refs"),
            ValidationIssue(validator="transition", code="invalid_transition", message="bad state"),
        )
        outcome.event_id = "evt_003"
        outcome.event_type = "observation.proposed"
        outcome.entity_type = "observation"
        outcome.entity_id = "obs_003"
        outcome.append_result = None

        trace = _trace_outcome(outcome)
        assert len(trace["rejection"]["all_issue_codes"]) == 3
        assert "schema_validation_failed" in trace["rejection"]["all_issue_codes"]
        assert "missing_source_refs" in trace["rejection"]["all_issue_codes"]
        assert "invalid_transition" in trace["rejection"]["all_issue_codes"]
