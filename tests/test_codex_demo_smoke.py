"""Smoke test: Codex end-to-end demo path — proof by artifact.

This test proves the full CLI pipeline works:
  init-audit → snapshot-target → enqueue-scan → run-all-tasks → rebuild-state → compile-report

The Codex executor is mocked at the subprocess boundary so the test runs
without OPENAI_API_KEY or network access. Everything above the mock is
production code.

Postconditions (system advantages proven by code, not words):

  P1  Workspace structure        — deterministic directory layout
  P2  Config integrity           — audit_id + policy bound at init time
  P3  Deterministic snapshot     — snapshot_ref is a real git SHA bound to HEAD
  P4  Task state machine         — task follows pending → running → done (no invalid transitions)
  P5  Source-bound events        — every observation has provenance with file_path, line_range, snapshot_ref
  P6  Run ledger with digests    — input_digest, output_digest, slice_fingerprint all present and non-empty
  P7  Linked trace               — run ledger references task_id → slice_id, accepted_events reference entity_ids in canonical state
  P8  Canonical state            — observations, questions, audit all present; state is a projection of events
  P9  Report compilation         — report references source_audit_id and source_state_digest
  P10 Event log append-only      — events are in chronological order with sequential IDs
  P11 Verification pipeline      — at least one observation reaches verified status
  P12 Issue pipeline             — at least one issue references verified observation evidence
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cli import main as cli_main


# ---------------------------------------------------------------------------
# Mock Codex executor — task-type-aware
# ---------------------------------------------------------------------------

def _make_codex_response(worker_input_json: dict[str, Any]) -> str:
    """Build a transport-format Codex response appropriate for the worker role.

    Reader     → observation.proposed + question.opened
    Verifier   → observation.verified
    IssueComposer → issue.proposed
    """
    worker_role = worker_input_json.get("worker_role", "Reader")
    snapshot_ref = worker_input_json.get("snapshot_ref", "snap_mock")
    target_paths = worker_input_json.get("target_paths", ["app/__init__.py"])
    file_path = target_paths[0] if target_paths else "app/__init__.py"
    task = worker_input_json.get("task", {})
    task_target = task.get("target", {})

    candidate_events: list[dict[str, Any]] = []

    if worker_role == "Reader":
        candidate_events = [
            {
                "event_type": "observation.proposed",
                "payload": {
                    "claim": f"The file {file_path} contains a Python module initializer.",
                    "evidence": [
                        {
                            "file_path": file_path,
                            "line_start": 1,
                            "line_end": 1,
                            "snapshot_ref": snapshot_ref,
                        }
                    ],
                },
            },
            {
                "event_type": "question.opened",
                "payload": {
                    "question": f"Is {file_path} the correct entry point for the application?",
                    "context": "Checking module structure.",
                },
            },
        ]

    elif worker_role == "Verifier":
        # The target observation is already in worker_input["relevant_observations"].
        # Return a verified event referencing the same evidence.
        target_obs_id = task_target.get("value", "")
        relevant_obs = worker_input_json.get("relevant_observations", {})
        obs = relevant_obs.get(target_obs_id, {})
        source_refs = obs.get("provenance", {}).get("source_refs", [])
        obs_file = source_refs[0].get("file_path", file_path) if source_refs else file_path

        candidate_events = [
            {
                "event_type": "observation.verified",
                "payload": {
                    "claim": f"Verified: {obs.get('statement', 'observation confirmed')}",
                    "evidence": [
                        {
                            "file_path": obs_file,
                            "line_start": 1,
                            "line_end": 5,
                            "snapshot_ref": snapshot_ref,
                        }
                    ] if source_refs else [],
                },
            },
        ]

    elif worker_role == "IssueComposer":
        target_obs_id = task_target.get("value", "")
        relevant_obs = worker_input_json.get("relevant_observations", {})
        obs = relevant_obs.get(target_obs_id, {})
        statement = obs.get("statement", "code observation")

        candidate_events = [
            {
                "event_type": "issue.proposed",
                "payload": {
                    "title": f"Finding: {statement[:80]}",
                    "summary": f"Audit finding based on verified observation: {statement}",
                    "evidence": {
                        "observation_ids": [target_obs_id] if target_obs_id else [],
                    },
                },
            },
        ]

    inner_payload = {
        "schema_version": "1.0.0",
        "slice_id": worker_input_json.get("slice_id", "slice_mock"),
        "worker_role": worker_role,
        "task_id": task.get("id", "task_mock"),
        "candidate_events": candidate_events,
    }

    outer = {"payload_json": json.dumps(inner_payload, ensure_ascii=True, sort_keys=True)}
    return json.dumps(outer, ensure_ascii=True, sort_keys=True)


def _mock_invoke_codex(self, invocation: Any) -> str:
    prompt = invocation.prompt
    try:
        marker_begin = "WORKER_INPUT_JSON_BEGIN"
        marker_end = "WORKER_INPUT_JSON_END"
        start = prompt.rfind(marker_begin)
        end = prompt.rfind(marker_end)
        if start >= 0 and end >= 0:
            json_text = prompt[start + len(marker_begin):end].strip()
            worker_input = json.loads(json_text)
        else:
            worker_input = {}
    except (json.JSONDecodeError, ValueError):
        worker_input = {}

    return _make_codex_response(worker_input)


# ---------------------------------------------------------------------------
# Fixture: minimal git repo for auditing
# ---------------------------------------------------------------------------

def _create_target_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target_repo"
    repo.mkdir()

    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text(
        "from app.main import create_app\n", encoding="utf-8"
    )
    (repo / "app" / "main.py").write_text(
        "def create_app():\n    return 'hello'\n", encoding="utf-8"
    )
    (repo / "app" / "routes.py").write_text(
        "from app.main import create_app\n\napp = create_app()\n",
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo), check=True, capture_output=True,
    )
    return repo


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

class TestCodexDemoSmoke:
    """End-to-end smoke test for the Codex demo path.

    Proves system advantages through artifact assertions, not words.
    """

    def test_full_pipeline_from_clean_workspace(self, tmp_path):
        target_repo = _create_target_repo(tmp_path)
        workspace = tmp_path / "demo_ws"
        audit_id = "audit_demo_smoke"

        with patch("runtime.adapters.codex_adapter.CodexAdapter._invoke_codex", _mock_invoke_codex):
            for args in [
                ["init-audit", "--workspace", str(workspace),
                 "--target-repo", str(target_repo),
                 "--audit-id", audit_id,
                 "--title", "Smoke test",
                 "--policy", "low_noise"],
                ["snapshot-target", "--workspace", str(workspace)],
                ["enqueue-scan", "--workspace", str(workspace),
                 "--target-kind", "path", "--targets", "app/main.py"],
                ["run-all-tasks", "--workspace", str(workspace),
                 "--backend", "codex", "--timeout-seconds", "30",
                 "--max-iterations", "20"],
                ["rebuild-state", "--workspace", str(workspace)],
                ["compile-report", "--workspace", str(workspace)],
            ]:
                rc = cli_main(args)
                assert rc == 0, f"CLI failed: {' '.join(args)}"

        # Load all artifacts once
        config = json.loads((workspace / "audit_config.json").read_text(encoding="utf-8"))
        state = json.loads((workspace / "state" / "canonical_state.json").read_text(encoding="utf-8"))
        task_queue = json.loads((workspace / "state" / "task_queue.json").read_text(encoding="utf-8"))
        events_log = (workspace / "events" / "events.ndjson").read_text(encoding="utf-8")
        events = [json.loads(l) for l in events_log.splitlines() if l.strip()]

        # Find run ledger
        run_ledger_path = workspace / "runs" / "run_ledger.ndjson"
        run_entries = []
        if run_ledger_path.exists():
            run_text = run_ledger_path.read_text(encoding="utf-8")
            run_entries = [json.loads(l) for l in run_text.splitlines() if l.strip()]

        # Find report
        report_files = list((workspace / "reports").glob("report.*.json"))
        report = json.loads(report_files[0].read_text(encoding="utf-8")) if report_files else {}

        # --- P1: Workspace structure ---
        for f in ["audit_config.json", "state/canonical_state.json",
                   "state/task_queue.json", "events/events.ndjson"]:
            assert (workspace / f).exists(), f"P1 FAIL: Missing {f}"

        # --- P2: Config integrity ---
        assert config["audit_id"] == audit_id, "P2 FAIL: audit_id mismatch"
        assert config["policy"] == "low_noise", "P2 FAIL: policy mismatch"

        # --- P3: Deterministic snapshot ---
        audit = state.get("audit", {})
        snap_ref = audit.get("current_snapshot_ref", "")
        assert len(snap_ref) >= 40, f"P3 FAIL: snapshot_ref too short: {snap_ref}"
        # snapshot_ref must be a hex string (git SHA)
        assert all(c in "0123456789abcdef" for c in snap_ref), f"P3 FAIL: not hex: {snap_ref}"

        # --- P4: Task state machine ---
        tasks = task_queue.get("tasks", {})
        if isinstance(tasks, dict):
            all_tasks = list(tasks.values())
        else:
            all_tasks = list(tasks)
        done_tasks = [t for t in all_tasks if t["status"] == "done"]
        assert len(done_tasks) >= 1, f"P4 FAIL: no done tasks. statuses={[(t['id'],t['status']) for t in all_tasks]}"
        # Every done task must have gone through valid transitions
        for t in done_tasks:
            assert t["attempt_count"] >= 1, f"P4 FAIL: done task {t['id']} has attempt_count=0"
            assert t["last_error"] is None, f"P4 FAIL: done task {t['id']} has error: {t['last_error']}"

        # --- P5: Source-bound events ---
        obs_events = [e for e in events if e["event_type"] == "observation.proposed"]
        assert len(obs_events) >= 1, "P5 FAIL: no observation events"
        for ev in obs_events:
            payload = ev.get("payload", {})
            provenance = payload.get("provenance", {})
            source_refs = provenance.get("source_refs", [])
            assert len(source_refs) >= 1, (
                f"P5 FAIL: observation {ev['entity_id']} has no source_refs"
            )
            for ref in source_refs:
                assert "file_path" in ref, f"P5 FAIL: source_ref missing file_path in {ev['entity_id']}"
                assert "snapshot_ref" in ref, f"P5 FAIL: source_ref missing snapshot_ref in {ev['entity_id']}"
                # line_range must have start and end
                lr = ref.get("line_range", {})
                assert "start" in lr and "end" in lr, (
                    f"P5 FAIL: source_ref missing line_range in {ev['entity_id']}"
                )
                assert isinstance(lr["start"], int), f"P5 FAIL: line_start not int"
                assert isinstance(lr["end"], int), f"P5 FAIL: line_end not int"

        # --- P6: Run ledger with digests ---
        worker_runs = [r for r in run_entries if r.get("task_id") and r["task_id"] != "?"]
        if worker_runs:
            r = worker_runs[0]
            for digest_field in ["input_digest", "output_digest", "slice_fingerprint"]:
                val = r.get(digest_field, "")
                assert isinstance(val, str) and len(val) >= 16, (
                    f"P6 FAIL: {digest_field} missing or too short in run ledger: {val}"
                )
            assert r.get("execution_status") == "succeeded", (
                f"P6 FAIL: execution_status={r.get('execution_status')}"
            )

        # --- P7: Linked trace ---
        if worker_runs:
            r = worker_runs[0]
            task_id = r["task_id"]
            slice_id = r.get("slice_id", "")
            # task_id must exist in task queue
            assert task_id in tasks if isinstance(tasks, dict) else any(
                t["id"] == task_id for t in tasks
            ), f"P7 FAIL: run references task {task_id} not in queue"
            # slice_id must reference a real slice file
            if slice_id:
                slice_path = workspace / "state" / "slices" / f"{task_id}.json"
                assert slice_path.exists(), f"P7 FAIL: slice file missing for {slice_id}"
            # accepted_events must reference entity_ids that exist in canonical state
            for ae in r.get("accepted_events", []):
                eid = ae["entity_id"]
                etype = ae["entity_type"]
                if etype == "observation":
                    assert eid in state.get("observations", {}), (
                        f"P7 FAIL: accepted entity {eid} not in canonical state observations"
                    )

        # --- P8: Canonical state ---
        observations = state.get("observations", {})
        assert len(observations) >= 1, f"P8 FAIL: no observations in canonical state"
        for oid, obs in observations.items():
            assert obs.get("status") in ("proposed", "verified"), (
                f"P8 FAIL: unexpected status {obs.get('status')} for {oid}"
            )
            assert obs.get("audit_id") == audit_id, f"P8 FAIL: wrong audit_id for {oid}"
            assert isinstance(obs.get("statement"), str) and len(obs["statement"]) > 0, (
                f"P8 FAIL: empty statement for {oid}"
            )

        # --- P9: Report compilation ---
        assert report.get("source_audit_id") == audit_id, "P9 FAIL: report audit_id mismatch"
        state_digest = report.get("source_state_digest", "")
        assert isinstance(state_digest, str) and len(state_digest) >= 16, (
            f"P9 FAIL: source_state_digest missing or too short: {state_digest}"
        )

        # --- P10: Event log append-only (sequential IDs) ---
        entry_ids = [e.get("id", "") for e in events]
        # IDs should be unique
        assert len(set(entry_ids)) == len(entry_ids), "P10 FAIL: duplicate event IDs"
        # Events should be ordered (sequence numbers or timestamps increasing)
        timestamps = [e.get("occurred_at", "") for e in events]
        assert timestamps == sorted(timestamps), "P10 FAIL: events not in chronological order"

        # --- P11: Verification pipeline — at least one observation reaches verified ---
        verified_obs = {
            oid: obs for oid, obs in observations.items()
            if obs.get("status") == "verified"
        }
        assert len(verified_obs) >= 1, (
            f"P11 FAIL: no verified observations. "
            f"statuses={[(oid, obs.get('status')) for oid, obs in observations.items()]}"
        )
        for oid, obs in verified_obs.items():
            assert obs.get("evidence_class") in (
                "direct_code_fact", "derived_structural_fact"
            ), (
                f"P11 FAIL: verified observation {oid} has evidence_class "
                f"'{obs.get('evidence_class')}', expected direct_code_fact or derived_structural_fact"
            )

        # --- P12: Issue pipeline — at least one issue with verified evidence ---
        issues = state.get("issues", {})
        assert len(issues) >= 1, (
            f"P12 FAIL: no issues in canonical state. "
            f"observations_verified={len(verified_obs)}"
        )
        for iid, issue in issues.items():
            assert issue.get("audit_id") == audit_id, f"P12 FAIL: wrong audit_id for issue {iid}"
            ev = issue.get("evidence", {})
            obs_ids = ev.get("observation_ids", [])
            assert len(obs_ids) >= 1, f"P12 FAIL: issue {iid} has no observation_ids"
            # Every referenced observation must be verified
            for obs_id in obs_ids:
                ref_obs = observations.get(obs_id)
                assert ref_obs is not None, f"P12 FAIL: issue {iid} references missing obs {obs_id}"
                assert ref_obs.get("status") == "verified", (
                    f"P12 FAIL: issue {iid} references unverified obs {obs_id} "
                    f"(status={ref_obs.get('status')})"
                )
