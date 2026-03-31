"""Lineage Completeness Audit Test for v1.2 Candidate Traceability.

This test verifies that every candidate can be deterministically traced through
its full lifecycle, including:
- Full promotion path (candidate.proposed -> routed -> promoted)
- Rejection path (candidate.proposed -> rejected)
- Pending path (candidate.proposed -> routed_to_verify, unresolved)
- Bidirectional link validation
"""

import tempfile
import shutil
from pathlib import Path

from runtime.run_ledger import RunLedger, WorkerExecutionTraceContext


def test_lineage_completeness():
    """Test all candidate lifecycle paths for traceability."""
    test_dir = tempfile.mkdtemp()
    try:
        ledger = RunLedger(root_dir=Path(test_dir))

        # Start a run
        run_result = ledger.start_run(audit_id='audit_001', snapshot_ref='snapshot_001')

        # Create candidate events for testing

        # Candidate 001: Full promotion path - proposed
        ledger.record_worker_execution(
            trace_context=WorkerExecutionTraceContext(
                run_id=run_result.run_id,
                audit_id='audit_001',
                task_id='task_001',
                slice_id='slice_001',
                worker_role='CandidateGenerator',
                adapter_invocation={},
                input_digest='abc123',
                output_digest='def456',
            ),
            total_candidate_events=1,
            accepted_events=1,
            rejected_events=0,
            event_outcomes=[
                {
                    'event_id': 'event_candidate_001_proposed',
                    'event_type': 'candidate.proposed',
                    'entity_type': 'candidate',
                    'entity_id': 'candidate_001',
                    'outcome': 'accepted',
                    'candidate_type': 'risk_candidate',
                }
            ],
            execution_status='succeeded',
        )

        # Candidate 001: routed_to_verify
        ledger.record_worker_execution(
            trace_context=WorkerExecutionTraceContext(
                run_id=run_result.run_id,
                audit_id='audit_001',
                task_id='task_002',
                slice_id='slice_002',
                worker_role='CandidateGenerator',
                adapter_invocation={},
                input_digest='ghi789',
                output_digest='jkl012',
            ),
            total_candidate_events=1,
            accepted_events=1,
            rejected_events=0,
            event_outcomes=[
                {
                    'event_id': 'event_candidate_001_routed',
                    'event_type': 'candidate.routed_to_verify',
                    'entity_type': 'candidate',
                    'entity_id': 'candidate_001',
                    'outcome': 'accepted',
                }
            ],
            execution_status='succeeded',
        )

        # Candidate 001: promoted_to_observation
        ledger.record_worker_execution(
            trace_context=WorkerExecutionTraceContext(
                run_id=run_result.run_id,
                audit_id='audit_001',
                task_id='task_003',
                slice_id='slice_003',
                worker_role='Verifier',
                adapter_invocation={},
                input_digest='mno345',
                output_digest='pqr678',
            ),
            total_candidate_events=1,
            accepted_events=1,
            rejected_events=0,
            event_outcomes=[
                {
                    'event_id': 'event_candidate_001_promoted',
                    'event_type': 'candidate.promoted_to_observation',
                    'entity_type': 'candidate',
                    'entity_id': 'candidate_001',
                    'outcome': 'accepted',
                    'promoted_observation_id': 'obs_001',
                }
            ],
            execution_status='succeeded',
        )

        # Candidate 002: Rejection path - proposed
        ledger.record_worker_execution(
            trace_context=WorkerExecutionTraceContext(
                run_id=run_result.run_id,
                audit_id='audit_001',
                task_id='task_004',
                slice_id='slice_004',
                worker_role='CandidateGenerator',
                adapter_invocation={},
                input_digest='stu901',
                output_digest='vwx234',
            ),
            total_candidate_events=1,
            accepted_events=1,
            rejected_events=0,
            event_outcomes=[
                {
                    'event_id': 'event_candidate_002_proposed',
                    'event_type': 'candidate.proposed',
                    'entity_type': 'candidate',
                    'entity_id': 'candidate_002',
                    'outcome': 'accepted',
                    'candidate_type': 'policy_candidate',
                }
            ],
            execution_status='succeeded',
        )

        # Candidate 002: rejected
        ledger.record_worker_execution(
            trace_context=WorkerExecutionTraceContext(
                run_id=run_result.run_id,
                audit_id='audit_001',
                task_id='task_005',
                slice_id='slice_005',
                worker_role='Verifier',
                adapter_invocation={},
                input_digest='yza567',
                output_digest='bcd890',
            ),
            total_candidate_events=1,
            accepted_events=1,
            rejected_events=0,
            event_outcomes=[
                {
                    'event_id': 'event_candidate_002_rejected',
                    'event_type': 'candidate.rejected',
                    'entity_type': 'candidate',
                    'entity_id': 'candidate_002',
                    'outcome': 'accepted',
                    'rejection_reason': 'Unable to verify claim',
                }
            ],
            execution_status='succeeded',
        )

        # Candidate 003: Pending (routed_to_verify, unresolved) - proposed
        ledger.record_worker_execution(
            trace_context=WorkerExecutionTraceContext(
                run_id=run_result.run_id,
                audit_id='audit_001',
                task_id='task_006',
                slice_id='slice_006',
                worker_role='CandidateGenerator',
                adapter_invocation={},
                input_digest='efg123',
                output_digest='hij456',
            ),
            total_candidate_events=1,
            accepted_events=1,
            rejected_events=0,
            event_outcomes=[
                {
                    'event_id': 'event_candidate_003_proposed',
                    'event_type': 'candidate.proposed',
                    'entity_type': 'candidate',
                    'entity_id': 'candidate_003',
                    'outcome': 'accepted',
                    'candidate_type': 'cross_file_correlation',
                }
            ],
            execution_status='succeeded',
        )

        # Candidate 003: routed_to_verify (unresolved)
        ledger.record_worker_execution(
            trace_context=WorkerExecutionTraceContext(
                run_id=run_result.run_id,
                audit_id='audit_001',
                task_id='task_007',
                slice_id='slice_007',
                worker_role='CandidateGenerator',
                adapter_invocation={},
                input_digest='klm789',
                output_digest='nop012',
            ),
            total_candidate_events=1,
            accepted_events=1,
            rejected_events=0,
            event_outcomes=[
                {
                    'event_id': 'event_candidate_003_routed',
                    'event_type': 'candidate.routed_to_verify',
                    'entity_type': 'candidate',
                    'entity_id': 'candidate_003',
                    'outcome': 'accepted',
                }
            ],
            execution_status='succeeded',
        )

        # Run tests
        _run_tests(ledger)

    finally:
        shutil.rmtree(test_dir)


def _run_tests(ledger: RunLedger):
    """Execute all lineage completeness tests."""
    print("=== TESTING LINEAGE COMPLETENESS ===")
    print()

    # Test 1: resolve_candidate_trace for promoted candidate
    trace1 = ledger.resolve_candidate_trace(audit_id='audit_001', candidate_id='candidate_001')
    print("Test 1: resolve_candidate_trace(candidate_001) - PROMOTED")
    print(f"  outcome: {trace1['outcome']}")
    print(f"  resolved: {trace1['resolved']}")
    print(f"  proposed_trace: {'YES' if trace1['proposed_trace'] else 'NO'}")
    print(f"  routed_trace: {'YES' if trace1['routed_trace'] else 'NO'}")
    print(f"  promotion_trace: {'YES' if trace1['promotion_trace'] else 'NO'}")
    assert trace1['outcome'] == 'resolved_promoted', f"Expected resolved_promoted, got {trace1['outcome']}"
    assert trace1['resolved'] is True, "Expected resolved=True"
    assert trace1['proposed_trace'] is not None, "Missing proposed_trace"
    assert trace1['routed_trace'] is not None, "Missing routed_trace"
    assert trace1['promotion_trace'] is not None, "Missing promotion_trace"
    print("  [PASS]")

    # Test 2: resolve_candidate_trace for rejected candidate
    trace2 = ledger.resolve_candidate_trace(audit_id='audit_001', candidate_id='candidate_002')
    print("Test 2: resolve_candidate_trace(candidate_002) - REJECTED")
    print(f"  outcome: {trace2['outcome']}")
    print(f"  resolved: {trace2['resolved']}")
    print(f"  proposed_trace: {'YES' if trace2['proposed_trace'] else 'NO'}")
    print(f"  rejection_trace: {'YES' if trace2['rejection_trace'] else 'NO'}")
    assert trace2['outcome'] == 'rejected', f"Expected rejected, got {trace2['outcome']}"
    assert trace2['resolved'] is True, "Expected resolved=True"
    assert trace2['proposed_trace'] is not None, "Missing proposed_trace"
    assert trace2['rejection_trace'] is not None, "Missing rejection_trace"
    print("  [PASS]")

    # Test 3: resolve_candidate_trace for pending candidate
    trace3 = ledger.resolve_candidate_trace(audit_id='audit_001', candidate_id='candidate_003')
    print("Test 3: resolve_candidate_trace(candidate_003) - PENDING")
    print(f"  outcome: {trace3['outcome']}")
    print(f"  resolved: {trace3['resolved']}")
    print(f"  proposed_trace: {'YES' if trace3['proposed_trace'] else 'NO'}")
    print(f"  routed_trace: {'YES' if trace3['routed_trace'] else 'NO'}")
    assert trace3['outcome'] == 'routed_to_verify', f"Expected routed_to_verify, got {trace3['outcome']}"
    assert trace3['resolved'] is False, "Expected resolved=False"
    assert trace3['proposed_trace'] is not None, "Missing proposed_trace"
    assert trace3['routed_trace'] is not None, "Missing routed_trace"
    print("  [PASS]")

    # Test 4: list_candidates_by_outcome
    all_candidates = ledger.list_candidates_by_outcome(audit_id='audit_001')
    print("Test 4: list_candidates_by_outcome(all)")
    print(f"  total candidates: {len(all_candidates)}")
    assert len(all_candidates) == 3, f"Expected 3 candidates, got {len(all_candidates)}"

    promoted = ledger.list_candidates_by_outcome(audit_id='audit_001', outcome='resolved_promoted')
    print(f"  promoted: {len(promoted)}")
    assert len(promoted) == 1, f"Expected 1 promoted, got {len(promoted)}"

    rejected = ledger.list_candidates_by_outcome(audit_id='audit_001', outcome='rejected')
    print(f"  rejected: {len(rejected)}")
    assert len(rejected) == 1, f"Expected 1 rejected, got {len(rejected)}"

    routed = ledger.list_candidates_by_outcome(audit_id='audit_001', outcome='routed_to_verify')
    print(f"  routed: {len(routed)}")
    assert len(routed) == 1, f"Expected 1 routed, got {len(routed)}"
    print("  [PASS]")

    # Test 5: resolve_candidate_forensic_trace
    forensic = ledger.resolve_candidate_forensic_trace(audit_id='audit_001', candidate_id='candidate_001')
    print("Test 5: resolve_candidate_forensic_trace(candidate_001)")
    print(f"  total events: {len(forensic['all_events'])}")
    print(f"  accepted: {forensic['accepted_count']}")
    print(f"  rejected: {forensic['rejected_count']}")
    print(f"  acceptance_rate: {forensic['acceptance_rate']:.2f}")
    assert len(forensic['all_events']) == 3, f"Expected 3 events, got {len(forensic['all_events'])}"
    assert forensic['accepted_count'] == 3, "Expected 3 accepted"
    assert forensic['acceptance_rate'] == 1.0, "Expected 100% acceptance"
    print("  [PASS]")

    # Test 6: resolve_candidate_lineage for promoted candidate
    lineage = ledger.resolve_candidate_lineage(audit_id='audit_001', candidate_id='candidate_001')
    print("Test 6: resolve_candidate_lineage(candidate_001)")
    print(f"  candidate_trace outcome: {lineage['candidate_trace']['outcome']}")
    print(f"  observation_lineage: {'YES' if lineage['observation_lineage'] else 'NO'}")
    print(f"  complete: {lineage['complete']}")
    assert lineage['candidate_trace']['outcome'] == 'resolved_promoted'
    assert lineage['candidate_trace']['resolved'] is True
    print("  [PASS]")

    # Test 7: verify event type matching in traces
    print("Test 7: Event type validation in traces")
    assert ledger._trace_matches_event_type(trace1['proposed_trace'], 'candidate.proposed')
    assert ledger._trace_matches_event_type(trace1['routed_trace'], 'candidate.routed_to_verify')
    assert ledger._trace_matches_event_type(trace1['promotion_trace'], 'candidate.promoted_to_observation')
    assert not ledger._trace_matches_event_type(None, 'candidate.proposed')
    assert not ledger._trace_matches_event_type({}, 'candidate.proposed')
    print("  [PASS]")

    # Test 8: verify rejection trace has correct event type
    print("Test 8: Rejection trace event type validation")
    assert ledger._trace_matches_event_type(trace2['rejection_trace'], 'candidate.rejected')
    print("  [PASS]")

    print()
    print("=== ALL LINEAGE COMPLETENESS TESTS PASSED ===")


if __name__ == '__main__':
    test_lineage_completeness()
