"""Test: OutputNormalizer transport-aware normalization.

Verify that the OutputNormalizer in ClaudeAgentSdkAdapter produces
candidate events with acceptance metadata BEFORE they reach _convert_to_codex_result.
"""

from __future__ import annotations

import json

from runtime.adapters.claude_agent_sdk_adapter import OutputNormalizer


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
}

RAW_OUTPUT_WITH_TRANSPORT = json.dumps({
    "schema_version": "1.0.0",
    "slice_id": "slice_abc123",
    "worker_role": "Reader",
    "task_id": "task_test",
    "candidate_events": [
        {
            "event_type": "observation.proposed",
            "payload": {
                "claim": "The app directory contains __init__.py.",
                "evidence": [
                    {
                        "file_path": "app/__init__.py",
                        "line_start": 1,
                        "line_end": 1,
                        "snapshot_ref": "snap1",
                    }
                ],
            },
        },
        {
            "event_type": "hypothesis.proposed",
            "payload": {
                "claim": "The app uses FastAPI.",
                "rationale": "Presence of routes/ directory.",
            },
        },
    ],
})

RAW_OUTPUT_FLAT_CLAUDE = json.dumps({
    "schema_version": "1.0.0",
    "slice_id": "slice_abc123",
    "worker_role": "Reader",
    "task_id": "task_test",
    "candidate_events": [
        {
            "event_type": "observation.proposed",
            "claim": "The app directory contains a Python package.",
            "evidence": {
                "file_path": "app",
                "line_start": 1,
                "line_end": 1,
                "snapshot_ref": "snap1",
            },
        },
    ],
})


class TestOutputNormalizerTransportAware:
    def test_transport_format_events_get_acceptance(self):
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            RAW_OUTPUT_WITH_TRANSPORT,
            worker_role="Reader",
            worker_input=WORKER_INPUT,
        )
        assert result.candidate_events, f"Expected events, got warnings: {result.parse_warnings}"
        for event in result.candidate_events:
            acc = event.get("acceptance")
            assert isinstance(acc, dict), f"Missing acceptance: {json.dumps(event, indent=2)[:300]}"
            assert acc["status"] == "pending"

    def test_flat_claude_events_get_acceptance(self):
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            RAW_OUTPUT_FLAT_CLAUDE,
            worker_role="Reader",
            worker_input=WORKER_INPUT,
        )
        assert result.candidate_events, f"Expected events, got warnings: {result.parse_warnings}"
        for event in result.candidate_events:
            acc = event.get("acceptance")
            assert isinstance(acc, dict), f"Missing acceptance: {json.dumps(event, indent=2)[:300]}"
            assert acc["status"] == "pending"

    def test_empty_candidate_events_produces_empty_list(self):
        normalizer = OutputNormalizer()
        raw = json.dumps({
            "schema_version": "1.0.0",
            "candidate_events": [],
        })
        result = normalizer.normalize(raw, worker_role="Reader", worker_input=WORKER_INPUT)
        assert result.candidate_events == []

    def test_transport_format_events_get_entity_ids(self):
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            RAW_OUTPUT_WITH_TRANSPORT,
            worker_role="Reader",
            worker_input=WORKER_INPUT,
        )
        assert len(result.candidate_events) == 2
        for event in result.candidate_events:
            assert isinstance(event.get("entity_id"), str), "Missing entity_id"
            assert isinstance(event.get("entity_type"), str), "Missing entity_type"
            assert event["entity_type"] in ("observation", "hypothesis")

    def test_events_get_actor_metadata(self):
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            RAW_OUTPUT_WITH_TRANSPORT,
            worker_role="Reader",
            worker_input=WORKER_INPUT,
        )
        for event in result.candidate_events:
            actor = event.get("actor")
            assert isinstance(actor, dict), "Missing actor"
            assert actor.get("actor_type") == "worker"
