"""Test: Claude format guard in _convert_to_codex_result.

Regression test for missing_acceptance_metadata rejections.
Claude returns flat events {event_type, claim, evidence} instead of
the transport format {event_type, payload: {claim, evidence: [...]}}.
The _ensure_transport_format method wraps Claude events into transport format
so that CodexAdapter._normalize_transport_candidate_event can process them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from runtime.adapters.claude_agent_sdk_adapter import ClaudeAgentSdkAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WORKER_INPUT = {
    "slice_id": "slice_abc123",
    "worker_role": "Reader",
    "task": {
        "id": "task_test",
        "audit_id": "audit_test",
        "type": "module_scan",
        "target": {"kind": "path", "value": "app", "snapshot_ref": "snap1"},
    },
    "snapshot_ref": "snap1",
    "target_paths": ["app"],
    "constraints": {},
}

CLAUDE_OBSERVATION = {
    "event_type": "observation.proposed",
    "claim": "The app directory contains a Python package.",
    "confidence": 1.0,
    "evidence": {
        "file_path": "app",
        "file_hash": "abc123",
        "line_start": 1,
        "line_end": 1,
        "snapshot_ref": "snap1",
    },
}

CLAUDE_HYPOTHESIS = {
    "event_type": "hypothesis.proposed",
    "claim": "The app uses a layered architecture.",
    "confidence": 0.7,
    "supporting_observations": [
        "The app directory contains a Python package."
    ],
}

TRANSPORT_FORMAT_EVENT = {
    "event_type": "observation.proposed",
    "payload": {
        "claim": "Test claim",
        "evidence": [
            {"file_path": "app.py", "line_start": 1, "line_end": 5, "snapshot_ref": "snap1"}
        ],
    },
}


# ---------------------------------------------------------------------------
# _ensure_transport_format unit tests
# ---------------------------------------------------------------------------

class TestEnsureTransportFormat:
    def test_claude_flat_observation_wraps_into_transport(self):
        result = ClaudeAgentSdkAdapter._ensure_transport_format(CLAUDE_OBSERVATION)
        assert "payload" in result
        assert isinstance(result["payload"], dict)
        assert result["payload"]["claim"] == "The app directory contains a Python package."
        assert isinstance(result["payload"]["evidence"], list)
        assert len(result["payload"]["evidence"]) == 1

    def test_claude_flat_hypothesis_wraps_into_transport(self):
        result = ClaudeAgentSdkAdapter._ensure_transport_format(CLAUDE_HYPOTHESIS)
        assert "payload" in result
        assert result["payload"]["claim"] == "The app uses a layered architecture."

    def test_transport_format_event_passes_through(self):
        result = ClaudeAgentSdkAdapter._ensure_transport_format(TRANSPORT_FORMAT_EVENT)
        assert result is TRANSPORT_FORMAT_EVENT

    def test_non_dict_passes_through(self):
        assert ClaudeAgentSdkAdapter._ensure_transport_format("string") == "string"
        assert ClaudeAgentSdkAdapter._ensure_transport_format(42) == 42

    def test_evidence_dict_becomes_list(self):
        event = {
            "event_type": "observation.proposed",
            "claim": "x",
            "evidence": {"file_path": "a.py", "line_start": 1, "line_end": 2, "snapshot_ref": "s"},
        }
        result = ClaudeAgentSdkAdapter._ensure_transport_format(event)
        assert isinstance(result["payload"]["evidence"], list)
        assert len(result["payload"]["evidence"]) == 1

    def test_confidence_excluded_from_payload(self):
        result = ClaudeAgentSdkAdapter._ensure_transport_format(CLAUDE_OBSERVATION)
        assert "confidence" not in result["payload"]
        assert "confidence" not in result  # not hoisted either

    def test_no_event_type_passes_through(self):
        event = {"claim": "something", "evidence": []}
        result = ClaudeAgentSdkAdapter._ensure_transport_format(event)
        assert result is event  # unchanged


# ---------------------------------------------------------------------------
# Integration: _convert_to_codex_result produces events with acceptance
# ---------------------------------------------------------------------------

class TestConvertToCodexResultAcceptance:
    """Verify that Claude-format events get acceptance metadata after conversion."""

    def _make_adapter(self) -> ClaudeAgentSdkAdapter:
        return ClaudeAgentSdkAdapter.__new__(ClaudeAgentSdkAdapter)

    def test_observation_gets_acceptance_metadata(self):
        from runtime.adapters.base import (
            BackendInvocationResult,
            BackendTelemetry,
        )

        adapter = self._make_adapter()
        result = BackendInvocationResult(
            success=True,
            payload={"schema_version": "1.0.0"},
            candidate_events=[CLAUDE_OBSERVATION],
            telemetry=BackendTelemetry(backend_type="claude_sdk"),
        )
        codex_result = adapter._convert_to_codex_result("Reader", WORKER_INPUT, result)

        assert len(codex_result.candidate_events) == 1
        event = codex_result.candidate_events[0]
        assert isinstance(event.get("acceptance"), dict), (
            f"Expected acceptance dict, got: {json.dumps(event, indent=2)[:500]}"
        )
        assert event["acceptance"]["status"] == "pending"

    def test_hypothesis_gets_acceptance_metadata(self):
        from runtime.adapters.base import (
            BackendInvocationResult,
            BackendTelemetry,
        )

        adapter = self._make_adapter()
        result = BackendInvocationResult(
            success=True,
            payload={"schema_version": "1.0.0"},
            candidate_events=[CLAUDE_HYPOTHESIS],
            telemetry=BackendTelemetry(backend_type="claude_sdk"),
        )
        codex_result = adapter._convert_to_codex_result("Reader", WORKER_INPUT, result)

        assert len(codex_result.candidate_events) == 1
        event = codex_result.candidate_events[0]
        assert isinstance(event.get("acceptance"), dict), (
            f"Expected acceptance dict, got: {json.dumps(event, indent=2)[:500]}"
        )
        assert event["acceptance"]["status"] == "pending"

    def test_multiple_events_all_get_acceptance(self):
        from runtime.adapters.base import (
            BackendInvocationResult,
            BackendTelemetry,
        )

        adapter = self._make_adapter()
        result = BackendInvocationResult(
            success=True,
            payload={"schema_version": "1.0.0"},
            candidate_events=[CLAUDE_OBSERVATION, CLAUDE_HYPOTHESIS],
            telemetry=BackendTelemetry(backend_type="claude_sdk"),
        )
        codex_result = adapter._convert_to_codex_result("Reader", WORKER_INPUT, result)

        assert len(codex_result.candidate_events) == 2
        for event in codex_result.candidate_events:
            assert isinstance(event.get("acceptance"), dict), (
                f"Missing acceptance: event_type={event.get('event_type')}"
            )
            assert event["acceptance"]["status"] == "pending"
