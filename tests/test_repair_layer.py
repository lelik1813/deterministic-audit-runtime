"""
Tests for Deterministic Repair Layer (STEP 5)

Tests the bounded repair logic for typed IR, ensuring:
1. Only classified repairs are applied
2. All repairs are logged
3. Non-repairable conditions raise RepairRequiredError
4. Semantic content is never created
"""

from __future__ import annotations

import pytest

from runtime.repair import (
    DeterministicRepairer,
    RepairContext,
    RepairLog,
    RepairLogEntry,
    RepairRequiredError,
    RepairType,
    RepairedTypedIR,
    create_repair_context,
    derive_entity_type,
    derive_status,
    get_status_derivation_map,
)


# Fixtures


@pytest.fixture
def repairer() -> DeterministicRepairer:
    """Create a DeterministicRepairer instance."""
    return DeterministicRepairer()


@pytest.fixture
def context() -> RepairContext:
    """Create a default repair context."""
    return create_repair_context(
        worker_role="Reader",
        trace_id="trace_test_001",
        slice_id="slice_001",
        task_id="task_001",
        snapshot_ref="snap_abc123",
        audit_id="audit_001",
        repaired_at="2026-03-27T12:00:00Z",
    )


@pytest.fixture
def minimal_event() -> dict:
    """Create a minimal valid event for testing."""
    return {
        "event_type": "observation.proposed",
        "entity_id": "obs_001",
    }


@pytest.fixture
def minimal_typed_output(minimal_event: dict) -> dict:
    """Create a minimal typed output for testing."""
    return {
        "worker_role": "Reader",
        "candidate_events": [minimal_event],
    }


# Tests for Entity Type Derivation


class TestEntityTypeDerivation:
    """Tests for entity_type derivation from event_type."""

    def test_derive_observation_types(self):
        """Observation events derive to 'observation'."""
        assert derive_entity_type("observation.proposed") == "observation"
        assert derive_entity_type("observation.verified") == "observation"
        assert derive_entity_type("observation.rejected") == "observation"

    def test_derive_hypothesis_types(self):
        """Hypothesis events derive to 'hypothesis'."""
        assert derive_entity_type("hypothesis.proposed") == "hypothesis"
        assert derive_entity_type("hypothesis.closed") == "hypothesis"
        assert derive_entity_type("hypothesis.rejected") == "hypothesis"

    def test_derive_issue_types(self):
        """Issue events derive to 'issue'."""
        assert derive_entity_type("issue.proposed") == "issue"
        assert derive_entity_type("issue.accepted") == "issue"
        assert derive_entity_type("issue.rejected") == "issue"
        assert derive_entity_type("issue.closed") == "issue"

    def test_derive_question_types(self):
        """Question events derive to 'question'."""
        assert derive_entity_type("question.opened") == "question"
        assert derive_entity_type("question.answered") == "question"
        assert derive_entity_type("question.closed") == "question"

    def test_derive_contradiction_types(self):
        """Contradiction events derive to 'contradiction'."""
        assert derive_entity_type("contradiction.registered") == "contradiction"
        assert derive_entity_type("contradiction.resolved") == "contradiction"

    def test_derive_candidate_types(self):
        """Candidate events derive to 'candidate'."""
        assert derive_entity_type("candidate.proposed") == "candidate"
        assert derive_entity_type("candidate.routed_to_verify") == "candidate"
        assert derive_entity_type("candidate.rejected") == "candidate"
        assert derive_entity_type("candidate.promoted_to_observation") == "candidate"

    def test_derive_unknown_event_type(self):
        """Unknown event_type returns None."""
        assert derive_entity_type("unknown.event") is None
        assert derive_entity_type("") is None

    def test_derive_null_event_type(self):
        """None event_type returns None."""
        assert derive_entity_type(None) is None


# Tests for Status Derivation


class TestStatusDerivation:
    """Tests for payload.status derivation from event_type."""

    def test_derive_observation_status(self):
        """Observation events derive status correctly."""
        assert derive_status("observation.proposed") == "proposed"
        assert derive_status("observation.verified") == "verified"
        assert derive_status("observation.rejected") == "rejected"

    def test_derive_hypothesis_status(self):
        """Hypothesis events derive status correctly."""
        assert derive_status("hypothesis.proposed") == "proposed"
        assert derive_status("hypothesis.sent_to_verification") == "in_verification"
        assert derive_status("hypothesis.supported") == "supported"
        assert derive_status("hypothesis.rejected") == "rejected"
        assert derive_status("hypothesis.unresolved_conflict") == "unresolved_conflict"

    def test_derive_issue_status(self):
        """Issue events derive status correctly."""
        assert derive_status("issue.proposed") == "proposed"
        assert derive_status("issue.accepted") == "accepted"
        assert derive_status("issue.rejected") == "rejected"
        assert derive_status("issue.closed") == "closed"

    def test_derive_question_status(self):
        """Question events derive status correctly."""
        assert derive_status("question.opened") == "open"
        assert derive_status("question.answered") == "answered"
        assert derive_status("question.closed") == "closed"

    def test_derive_contradiction_status(self):
        """Contradiction events derive status correctly."""
        assert derive_status("contradiction.registered") == "registered"
        assert derive_status("contradiction.resolved") == "resolved"

    def test_derive_decision_status(self):
        """Decision events always derive to 'recorded'."""
        assert derive_status("decision.recorded") == "recorded"

    def test_derive_candidate_status(self):
        """Candidate events derive status correctly."""
        assert derive_status("candidate.proposed") == "proposed"
        assert derive_status("candidate.routed_to_verify") == "routed_to_verify"
        assert derive_status("candidate.rejected") == "rejected"
        assert derive_status("candidate.promoted_to_observation") == "promoted_to_observation"

    def test_derive_unknown_event_type(self):
        """Unknown event_type returns None."""
        assert derive_status("unknown.event") is None
        assert derive_status("") is None

    def test_derive_null_event_type(self):
        """None event_type returns None."""
        assert derive_status(None) is None

    def test_status_derivation_map_complete(self):
        """All state-transition events have status mappings."""
        status_map = get_status_derivation_map()
        # Verify key events are mapped
        assert "observation.verified" in status_map
        assert "observation.rejected" in status_map
        assert "hypothesis.supported" in status_map
        assert "issue.proposed" in status_map


# Tests for Status Derivation Repair


class TestStatusDerivationRepair:
    """Tests for STATUS_DERIVATION repair type."""

    def test_derive_missing_status_for_observation_verified(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """REGRESSION: observation.verified without payload.status gets repaired."""
        event = {
            "event_type": "observation.verified",
            "entity_id": "obs_001",
            "payload": {"id": "obs_001"},  # Missing status!
        }
        output = {"worker_role": "Verifier", "candidate_events": [event]}

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        repaired_event = result.typed_output["candidate_events"][0]
        assert repaired_event["payload"]["status"] == "verified"

        # Check log entry
        status_repairs = [
            e for e in result.repair_log.entries if e.field_path == "payload.status"
        ]
        assert len(status_repairs) >= 1
        status_derivation = [e for e in status_repairs if e.repair_type == RepairType.STATUS_DERIVATION]
        assert len(status_derivation) >= 1
        assert status_derivation[0].repair_source == "derivation"
        assert status_derivation[0].repaired_value == "verified"

    def test_derive_missing_status_for_observation_rejected(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """observation.rejected without payload.status gets repaired."""
        event = {
            "event_type": "observation.rejected",
            "entity_id": "obs_001",
            "payload": {"id": "obs_001"},  # Missing status!
        }
        output = {"worker_role": "Verifier", "candidate_events": [event]}

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        repaired_event = result.typed_output["candidate_events"][0]
        assert repaired_event["payload"]["status"] == "rejected"

    def test_derive_missing_status_for_hypothesis_supported(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """hypothesis.supported without payload.status gets repaired."""
        event = {
            "event_type": "hypothesis.supported",
            "entity_id": "hyp_001",
            "payload": {"id": "hyp_001"},  # Missing status!
        }
        output = {"worker_role": "Verifier", "candidate_events": [event]}

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        repaired_event = result.typed_output["candidate_events"][0]
        assert repaired_event["payload"]["status"] == "supported"

    def test_no_derivation_when_status_present(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """No derivation when payload.status is already present."""
        event = {
            "event_type": "observation.verified",
            "entity_id": "obs_001",
            "payload": {"id": "obs_001", "status": "custom_status"},
        }
        output = {"worker_role": "Verifier", "candidate_events": [event]}

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        repaired_event = result.typed_output["candidate_events"][0]
        # Should NOT be overwritten
        assert repaired_event["payload"]["status"] == "custom_status"

    def test_derivation_creates_payload_when_missing(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """Status derivation creates payload dict if missing entirely."""
        event = {
            "event_type": "observation.verified",
            "entity_id": "obs_001",
            # No payload at all
        }
        output = {"worker_role": "Verifier", "candidate_events": [event]}

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        # Should create payload with derived status
        repaired_event = result.typed_output["candidate_events"][0]
        assert "payload" in repaired_event
        assert repaired_event["payload"]["status"] == "verified"

    def test_derive_null_status_to_derived(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """Null payload.status is replaced with derived value."""
        event = {
            "event_type": "observation.verified",
            "entity_id": "obs_001",
            "payload": {"id": "obs_001", "status": None},
        }
        output = {"worker_role": "Verifier", "candidate_events": [event]}

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        repaired_event = result.typed_output["candidate_events"][0]
        assert repaired_event["payload"]["status"] == "verified"


# Tests for Default Injection


class TestDefaultInjection:
    """Tests for DEFAULT_INJECTION repair type."""

    def test_inject_missing_acceptance(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """Missing acceptance gets injected with pending structure."""
        # Remove acceptance
        minimal_typed_output["candidate_events"][0].pop("acceptance", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        event = result.typed_output["candidate_events"][0]
        assert event["acceptance"] == {
            "status": "pending",
            "decided_at": None,
            "decided_by": None,
            "reason": None,
        }

        # Check log entry
        acceptance_repairs = [
            e for e in result.repair_log.entries if e.field_path == "acceptance"
        ]
        assert len(acceptance_repairs) == 1
        assert acceptance_repairs[0].repair_type == RepairType.DEFAULT_INJECTION
        assert acceptance_repairs[0].repair_source == "fixed_default"

    def test_inject_missing_schema_version(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """Missing schema_version gets injected with '1.0.0'."""
        minimal_typed_output.pop("schema_version", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        assert result.typed_output["schema_version"] == "1.0.0"

        # Check log entry
        version_repairs = [
            e for e in result.repair_log.entries if e.field_path == "schema_version"
        ]
        assert len(version_repairs) == 1
        assert version_repairs[0].repair_type == RepairType.DEFAULT_INJECTION


# Tests for Null Normalization


class TestNullNormalization:
    """Tests for NULL_NORMALIZATION repair type."""

    def test_normalize_string_null_to_none(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """String 'null' is normalized to None, then entity_type is derived."""
        minimal_typed_output["candidate_events"][0]["entity_type"] = "null"

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        event = result.typed_output["candidate_events"][0]
        # After null normalization, entity_type derivation runs
        assert event["entity_type"] == "observation"

        # Check log entries - should have both normalization and derivation
        null_repairs = [
            e
            for e in result.repair_log.entries
            if e.field_path == "entity_type" and e.repair_type == RepairType.NULL_NORMALIZATION
        ]
        assert len(null_repairs) == 1
        assert null_repairs[0].original_value == "null"
        assert null_repairs[0].repaired_value is None

        derivation_repairs = [
            e
            for e in result.repair_log.entries
            if e.field_path == "entity_type" and e.repair_type == RepairType.ENTITY_TYPE_DERIVATION
        ]
        assert len(derivation_repairs) == 1

    def test_normalize_empty_string_decided_at(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """Empty string in acceptance.decided_at is normalized to None."""
        minimal_typed_output["candidate_events"][0]["acceptance"] = {
            "status": "pending",
            "decided_at": "",
            "decided_by": None,
            "reason": None,
        }

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        event = result.typed_output["candidate_events"][0]
        assert event["acceptance"]["decided_at"] is None

        # Check log entry
        decided_repairs = [
            e for e in result.repair_log.entries if e.field_path == "acceptance.decided_at"
        ]
        assert len(decided_repairs) == 1
        assert decided_repairs[0].repair_type == RepairType.NULL_NORMALIZATION


# Tests for Context Injection


class TestContextInjection:
    """Tests for CONTEXT_INJECTION repair type."""

    def test_inject_snapshot_ref_from_context(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """Missing snapshot_ref is injected from context."""
        minimal_typed_output["candidate_events"][0].pop("snapshot_ref", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        event = result.typed_output["candidate_events"][0]
        assert event["snapshot_ref"] == "snap_abc123"

        # Check log entry
        snap_repairs = [
            e for e in result.repair_log.entries if e.field_path == "snapshot_ref"
        ]
        assert len(snap_repairs) >= 1
        context_injections = [e for e in snap_repairs if e.repair_type == RepairType.CONTEXT_INJECTION]
        assert len(context_injections) >= 1
        assert context_injections[0].repair_source == "context"

    def test_no_injection_when_context_null(
        self,
        repairer: DeterministicRepairer,
        minimal_typed_output: dict,
    ):
        """No injection when context value is None."""
        context_no_snap = create_repair_context(
            worker_role="Reader",
            trace_id="trace_test_002",
            snapshot_ref=None,
        )

        minimal_typed_output["candidate_events"][0].pop("snapshot_ref", None)

        result = repairer.repair(minimal_typed_output, context_no_snap)

        assert isinstance(result, RepairedTypedIR)
        event = result.typed_output["candidate_events"][0]
        # snapshot_ref should not be injected
        assert event.get("snapshot_ref") is None


# Tests for Entity Type Derivation Repair


class TestEntityTypeDerivationRepair:
    """Tests for ENTITY_TYPE_DERIVATION repair type."""

    def test_derive_entity_type_from_event_type(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """entity_type is derived from event_type when missing."""
        minimal_typed_output["candidate_events"][0].pop("entity_type", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        event = result.typed_output["candidate_events"][0]
        assert event["entity_type"] == "observation"

        # Check log entry
        derivation_repairs = [
            e for e in result.repair_log.entries if e.field_path == "entity_type"
        ]
        assert len(derivation_repairs) >= 1
        derivations = [e for e in derivation_repairs if e.repair_type == RepairType.ENTITY_TYPE_DERIVATION]
        assert len(derivations) >= 1
        assert derivations[0].repair_source == "derivation"

    def test_no_derivation_when_entity_type_present(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """No derivation when entity_type is already present."""
        minimal_typed_output["candidate_events"][0]["entity_type"] = "custom_type"

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        event = result.typed_output["candidate_events"][0]
        assert event["entity_type"] == "custom_type"


# Tests for Non-Repairable Conditions


class TestNonRepairableConditions:
    """Tests for non-repairable conditions that raise RepairRequiredError."""

    def test_missing_event_type(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """Missing event_type raises RepairRequiredError."""
        minimal_typed_output["candidate_events"][0].pop("event_type", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairRequiredError)
        assert result.failure_code == "missing_event_type"
        assert result.retryable is True

    def test_acceptance_not_pending(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """acceptance.status != 'pending' raises RepairRequiredError."""
        minimal_typed_output["candidate_events"][0]["acceptance"] = {
            "status": "accepted",
            "decided_at": "2026-03-27T12:00:00Z",
            "decided_by": {"actor_type": "system", "actor_id": "test"},
            "reason": "test",
        }

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairRequiredError)
        assert result.failure_code == "candidate_event_not_pending"
        assert result.retryable is False  # Fatal

    def test_invalid_acceptance_actor(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """decided_by present with pending status raises RepairRequiredError."""
        minimal_typed_output["candidate_events"][0]["acceptance"] = {
            "status": "pending",
            "decided_at": None,
            "decided_by": {"actor_type": "system", "actor_id": "invalid"},
            "reason": None,
        }

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairRequiredError)
        assert result.failure_code == "invalid_acceptance_actor"
        assert result.retryable is False  # Fatal


# Tests for Repair Logging


class TestRepairLogging:
    """Tests for repair log generation."""

    def test_all_repairs_logged(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """All repairs are logged with trace correlation."""
        # Remove multiple fields to trigger multiple repairs
        minimal_typed_output.pop("schema_version", None)
        minimal_typed_output["candidate_events"][0].pop("acceptance", None)
        minimal_typed_output["candidate_events"][0].pop("entity_type", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        assert result.repair_log.total_repairs >= 3
        assert result.repair_log.trace_id == context.trace_id
        assert result.repair_log.worker_role == context.worker_role

        # All entries have trace_id
        for entry in result.repair_log.entries:
            assert entry.trace_id == context.trace_id
            assert entry.repair_id.startswith("repair_")

    def test_repairs_by_type_count(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """Repairs are counted by type."""
        minimal_typed_output.pop("schema_version", None)
        minimal_typed_output["candidate_events"][0].pop("entity_type", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        assert result.repair_log.repairs_by_type[RepairType.DEFAULT_INJECTION] >= 1
        assert result.repair_log.repairs_by_type[RepairType.ENTITY_TYPE_DERIVATION] >= 1


# Tests for Serialization


class TestSerialization:
    """Tests for to_dict() methods."""

    def test_repair_log_entry_to_dict(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """RepairLogEntry serializes correctly."""
        minimal_typed_output.pop("schema_version", None)

        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        entry_dict = result.repair_log.entries[0].to_dict()
        assert "repair_id" in entry_dict
        assert "trace_id" in entry_dict
        assert "repair_type" in entry_dict
        assert entry_dict["repair_type"] in ["default_injection", "null_normalization", "context_injection", "entity_type_derivation", "status_derivation"]

    def test_repair_log_to_dict(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """RepairLog serializes correctly."""
        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        log_dict = result.repair_log.to_dict()
        assert "trace_id" in log_dict
        assert "worker_role" in log_dict
        assert "total_repairs" in log_dict
        assert "repairs_by_type" in log_dict
        assert "entries" in log_dict

    def test_repaired_typed_ir_to_dict(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """RepairedTypedIR serializes correctly."""
        result = repairer.repair(minimal_typed_output, context)

        assert isinstance(result, RepairedTypedIR)
        result_dict = result.to_dict()
        assert "typed_output" in result_dict
        assert "repair_log" in result_dict
        assert result_dict["repair_success"] is True


# Tests for Edge Cases


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_candidate_events(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """Empty candidate_events list is handled with worker-level repairs."""
        output = {"worker_role": "Reader", "candidate_events": []}

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        # Worker-level repairs still apply (schema_version, slice_id, task_id, snapshot_ref)
        assert result.repair_log.total_repairs >= 1
        # No event-level repairs since there are no events
        event_repairs = [e for e in result.repair_log.entries if e.event_index >= 0]
        assert len(event_repairs) == 0

    def test_multiple_events(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
    ):
        """Multiple events are repaired independently."""
        output = {
            "worker_role": "Reader",
            "candidate_events": [
                {"event_type": "observation.proposed", "entity_id": "obs_001"},
                {"event_type": "issue.proposed", "entity_id": "issue_001"},
            ],
        }

        result = repairer.repair(output, context)

        assert isinstance(result, RepairedTypedIR)
        events = result.typed_output["candidate_events"]
        assert events[0]["entity_type"] == "observation"
        assert events[1]["entity_type"] == "issue"

    def test_input_not_mutated(
        self,
        repairer: DeterministicRepairer,
        context: RepairContext,
        minimal_typed_output: dict,
    ):
        """Original input dict is not mutated."""
        original = dict(minimal_typed_output)
        original_event = dict(minimal_typed_output["candidate_events"][0])

        repairer.repair(minimal_typed_output, context)

        # Input should not be mutated
        assert minimal_typed_output["worker_role"] == original["worker_role"]


# Tests for RepairRequiredError


class TestRepairRequiredError:
    """Tests for RepairRequiredError."""

    def test_error_attributes(self):
        """RepairRequiredError has required attributes."""
        error = RepairRequiredError(
            failure_code="test_failure",
            field_path="test.field",
            message="Test message",
            event_index=0,
            retryable=True,
        )

        assert error.failure_code == "test_failure"
        assert error.field_path == "test.field"
        assert error.message == "Test message"
        assert error.event_index == 0
        assert error.retryable is True

    def test_error_to_dict(self):
        """RepairRequiredError serializes to dict."""
        error = RepairRequiredError(
            failure_code="test_failure",
            field_path="test.field",
            message="Test message",
            event_index=0,
            retryable=True,
        )

        d = error.to_dict()
        assert d["failure_code"] == "test_failure"
        assert d["field_path"] == "test.field"
        assert d["retryable"] is True
