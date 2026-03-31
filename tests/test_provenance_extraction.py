"""
Regression test for nested provenance extraction (source binding fix).

Root cause: runtime/processing.py extracted source_refs from wrong path.
Model generates: payload.provenance.source_refs
Runtime looked at: event.source_refs (top-level) → always empty

This test verifies the fix extracts from the correct nested path.
"""

import pytest
from runtime.processing import _build_observation_payload


class TestNestedProvenanceExtraction:
    """Test that source_refs are extracted from provenance (model output structure)."""

    def test_extracts_source_refs_from_nested_provenance(self):
        """
        Model generates provenance.source_refs (nested structure).
        _build_payload_from_event must extract from this path and pass to _build_observation_payload.
        """
        from runtime.processing import _build_payload_from_event

        event = {
            "event_type": "observation.proposed",
            "entity_type": "observation",
            "entity_id": "obs_test_001",
            "audit_id": "audit_test",
            "occurred_at": "2026-03-28T00:00:00Z",
            "snapshot_ref": "abc123",
            "statement": "Test observation",
            # Model output has provenance with nested source_refs
            "provenance": {
                "source_refs": [
                    {
                        "file_path": "src/example.py",
                        "line_range": {"start": 10, "end": 20},
                        "snapshot_ref": "abc123"
                    }
                ]
            }
        }

        # Go through _build_payload_from_event which does the extraction
        payload = _build_payload_from_event(event)

        # Should NOT have placeholder
        source_refs = payload["provenance"]["source_refs"]
        assert len(source_refs) == 1
        assert source_refs[0]["file_path"] == "src/example.py", \
            f"Expected 'src/example.py', got '{source_refs[0]['file_path']}'"
        assert source_refs[0]["line_range"]["start"] == 10
        assert source_refs[0]["file_path"] != "unknown", \
            "Source binding failed - nested provenance.source_refs not extracted"

    def test_fallback_to_top_level_source_refs(self):
        """
        Backward compatibility: if provenance.source_refs not present,
        fall back to top-level source_refs.
        """
        event = {
            "event_type": "observation.proposed",
            "entity_type": "observation",
            "entity_id": "obs_test_002",
            "audit_id": "audit_test",
            "occurred_at": "2026-03-28T00:00:00Z",
            "snapshot_ref": "abc123",
            "statement": "Test observation",
            # Top-level source_refs (old structure)
            "source_refs": [
                {
                    "file_path": "src/legacy.py",
                    "line_range": {"start": 1, "end": 5},
                    "snapshot_ref": "abc123"
                }
            ]
        }

        payload = _build_observation_payload(
            entity_id="obs_test_002",
            audit_id="audit_test",
            event=event,
            occurred_at="2026-03-28T00:00:00Z",
            source_refs=event["source_refs"],  # Passed from extraction
            snapshot_ref="abc123"
        )

        source_refs = payload["provenance"]["source_refs"]
        assert len(source_refs) == 1
        assert source_refs[0]["file_path"] == "src/legacy.py"

    def test_no_provenance_creates_placeholder(self):
        """
        If model provides neither provenance.source_refs nor top-level source_refs,
        create placeholder (this is expected behavior for malformed output).
        """
        event = {
            "event_type": "observation.proposed",
            "entity_type": "observation",
            "entity_id": "obs_test_003",
            "audit_id": "audit_test",
            "occurred_at": "2026-03-28T00:00:00Z",
            "snapshot_ref": "abc123",
            "statement": "Test observation",
            # No source_refs at all
        }

        payload = _build_observation_payload(
            entity_id="obs_test_003",
            audit_id="audit_test",
            event=event,
            occurred_at="2026-03-28T00:00:00Z",
            source_refs=[],  # Empty
            snapshot_ref="abc123"
        )

        # Should create placeholder
        source_refs = payload["provenance"]["source_refs"]
        assert len(source_refs) == 1
        assert source_refs[0]["file_path"] == "unknown"


class TestCompleteCandidateEventProvenance:
    """End-to-end test of provenance extraction through _complete_candidate_event."""

    def test_complete_event_extracts_nested_provenance(self):
        """
        Full pipeline: model output → _complete_candidate_event → final event.
        Verify source_refs survive the transformation.
        """
        from runtime.processing import _complete_candidate_event

        # Model output structure
        candidate_event = {
            "event_type": "observation.proposed",
            "entity_id": "obs_integration_test",
            "payload": {
                "statement": "The function validates input",
                "evidence_class": "direct_code_fact",
                "provenance": {
                    "source_refs": [
                        {
                            "file_path": "src/validators.py",
                            "line_range": {"start": 45, "end": 52},
                            "snapshot_ref": "test_snapshot_ref"
                        }
                    ]
                }
            },
            "acceptance": {
                "status": "pending",
                "decided_at": None,
                "decided_by": None,
                "reason": None
            }
        }

        completed = _complete_candidate_event(
            candidate_event,
            audit_id="audit_integration_test",
            snapshot_ref="test_snapshot_ref"
        )

        # Verify source_refs preserved
        source_refs = completed["payload"]["provenance"]["source_refs"]
        assert len(source_refs) == 1
        assert source_refs[0]["file_path"] == "src/validators.py", \
            f"Expected src/validators.py, got {source_refs[0]['file_path']}"
        assert source_refs[0]["file_path"] != "unknown", \
            "Source binding failed - got placeholder 'unknown' instead of actual file path"

    def test_no_regression_on_empty_provenance(self):
        """
        Regression guard: ensure empty/missing provenance still creates valid placeholder.
        This should NOT break - it's valid fallback behavior.
        """
        from runtime.processing import _complete_candidate_event

        candidate_event = {
            "event_type": "question.opened",
            "entity_id": "q_test",
            "payload": {
                "prompt": "What does this function do?",
                "context": None
            },
            "acceptance": {
                "status": "pending",
                "decided_at": None,
                "decided_by": None,
                "reason": None
            }
        }

        completed = _complete_candidate_event(
            candidate_event,
            audit_id="audit_test",
            snapshot_ref="test_ref"
        )

        # Questions don't require provenance, should complete successfully
        assert completed["entity_type"] == "question"
        assert "payload" in completed


class TestSourceBindingContract:
    """
    Contract tests: verify the source binding contract is upheld.

    Contract: Every observation.proposed MUST have valid source_refs
    with actual file_path (not 'unknown') when the model provides them.
    """

    def test_model_provided_source_refs_not_discarded(self):
        """
        INVARIANT: Model-provided source_refs MUST NOT be discarded.
        If model provides payload.provenance.source_refs, they must appear in final event.
        """
        from runtime.processing import _complete_candidate_event

        candidate_event = {
            "event_type": "observation.proposed",
            "entity_id": "obs_contract_test",
            "payload": {
                "statement": "Contract test observation",
                "evidence_class": "direct_code_fact",
                "provenance": {
                    "source_refs": [
                        {
                            "file_path": "contract/target.py",
                            "line_range": {"start": 100, "end": 110},
                            "snapshot_ref": "contract_snapshot"
                        }
                    ]
                }
            },
            "acceptance": {
                "status": "pending",
                "decided_at": None,
                "decided_by": None,
                "reason": None
            }
        }

        completed = _complete_candidate_event(
            candidate_event,
            audit_id="audit_contract",
            snapshot_ref="contract_snapshot"
        )

        source_refs = completed["payload"]["provenance"]["source_refs"]

        # CONTRACT ASSERTION: source_refs must be preserved
        assert len(source_refs) == 1, \
            f"Expected 1 source_ref, got {len(source_refs)}"
        assert source_refs[0]["file_path"] == "contract/target.py", \
            f"Source binding contract violated: expected 'contract/target.py', got '{source_refs[0]['file_path']}'"
        assert source_refs[0]["file_path"] != "unknown", \
            "CRITICAL: Source binding contract violated - model-provided source_refs were replaced with placeholder"
