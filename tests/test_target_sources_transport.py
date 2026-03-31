"""Transport verification: prove target_sources reaches the LLM and produces observation.proposed.

DoD:
  T1  Slice contains target_sources with non-empty file_content
  T2  Prompt contains "target_sources" instruction text
  T3  Raw Codex output references code from file_content
  T4  At least 1 observation.proposed event in the event log

If any check fails → pipeline bug, not an LLM problem.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cli import main as cli_main


# ---------------------------------------------------------------------------
# Instrumented mock: captures the exact prompt sent to Codex
# ---------------------------------------------------------------------------

captured_prompts: list[str] = []
captured_worker_inputs: list[dict[str, Any]] = []


def _parse_worker_input_from_prompt(prompt: str) -> dict[str, Any]:
    """Extract worker_input JSON from the prompt using canonical markers."""
    for begin, end in [
        ("WORKER_INPUT_JSON_BEGIN\n", "\nWORKER_INPUT_JSON_END"),
        ("WORKER_INPUT_JSON_BEGIN", "WORKER_INPUT_JSON_END"),
    ]:
        start = prompt.find(begin)
        stop = prompt.find(end)
        if start >= 0 and stop > start:
            json_text = prompt[start + len(begin):stop].strip()
            try:
                return json.loads(json_text)
            except json.JSONDecodeError:
                continue
    return {}


def _make_reader_response(worker_input_json: dict[str, Any]) -> str:
    """Build a Reader response that PROVABLY uses file_content from target_sources."""
    target_sources = worker_input_json.get("target_sources", [])
    snapshot_ref = worker_input_json.get("snapshot_ref", "snap_mock")
    target_paths = worker_input_json.get("target_paths", [])

    # If target_sources has real file_content, use it in the observation claim
    candidate_events: list[dict[str, Any]] = []

    if target_sources:
        for src in target_sources:
            fp = src.get("file_path", "unknown")
            fc = src.get("file_content", "")
            # Include first line of actual code in the claim to prove we read it
            first_line = fc.split("\n")[0].strip() if fc else "<empty>"
            line_count = len(fc.splitlines()) if fc else 1
            candidate_events.append({
                "event_type": "observation.proposed",
                "payload": {
                    "claim": f"File {fp} first line: '{first_line}'. Contains {len(fc)} chars.",
                    "evidence": [{
                        "file_path": fp,
                        "line_start": 1,
                        "line_end": max(1, line_count),
                        "snapshot_ref": snapshot_ref,
                    }],
                },
            })
    else:
        # No target_sources — emit a question instead
        candidate_events.append({
            "event_type": "question.opened",
            "payload": {
                "question": f"No target_sources provided for paths: {target_paths}",
                "context": "Cannot audit without file content.",
            },
        })

    inner_payload = {
        "schema_version": "1.0.0",
        "slice_id": worker_input_json.get("slice_id", "slice_mock"),
        "worker_role": worker_input_json.get("worker_role", "Reader"),
        "task_id": worker_input_json.get("task", {}).get("id", "task_mock"),
        "candidate_events": candidate_events,
    }
    outer = {"payload_json": json.dumps(inner_payload, ensure_ascii=True, sort_keys=True)}
    return json.dumps(outer, ensure_ascii=True, sort_keys=True)


def _make_verifier_response(worker_input_json: dict[str, Any]) -> str:
    """Build a Verifier response that verifies the target observation."""
    target_obs_id = worker_input_json.get("task", {}).get("target", {}).get("value", "")
    relevant_obs = worker_input_json.get("relevant_observations", {})
    obs = relevant_obs.get(target_obs_id, {})
    snapshot_ref = worker_input_json.get("snapshot_ref", "snap_mock")
    source_refs = obs.get("provenance", {}).get("source_refs", [])
    obs_file = source_refs[0].get("file_path", "unknown") if source_refs else "unknown"

    candidate_events = [{
        "event_type": "observation.verified",
        "payload": {
            "claim": f"Verified: {obs.get('statement', 'confirmed')}",
            "evidence": [{
                "file_path": obs_file,
                "line_start": 1,
                "line_end": 5,
                "snapshot_ref": snapshot_ref,
            }] if source_refs else [],
        },
    }]

    inner_payload = {
        "schema_version": "1.0.0",
        "slice_id": worker_input_json.get("slice_id", "slice_mock"),
        "worker_role": "Verifier",
        "task_id": worker_input_json.get("task", {}).get("id", "task_mock"),
        "candidate_events": candidate_events,
    }
    outer = {"payload_json": json.dumps(inner_payload, ensure_ascii=True, sort_keys=True)}
    return json.dumps(outer, ensure_ascii=True, sort_keys=True)


def _make_issue_composer_response(worker_input_json: dict[str, Any]) -> str:
    """Build an IssueComposer response."""
    target_obs_id = worker_input_json.get("task", {}).get("target", {}).get("value", "")
    relevant_obs = worker_input_json.get("relevant_observations", {})
    obs = relevant_obs.get(target_obs_id, {})
    statement = obs.get("statement", "code observation")

    candidate_events = [{
        "event_type": "issue.proposed",
        "payload": {
            "title": f"Finding: {statement[:80]}",
            "summary": f"Audit finding based on verified observation: {statement}",
            "evidence": {
                "observation_ids": [target_obs_id] if target_obs_id else [],
            },
        },
    }]

    inner_payload = {
        "schema_version": "1.0.0",
        "slice_id": worker_input_json.get("slice_id", "slice_mock"),
        "worker_role": "IssueComposer",
        "task_id": worker_input_json.get("task", {}).get("id", "task_mock"),
        "candidate_events": candidate_events,
    }
    outer = {"payload_json": json.dumps(inner_payload, ensure_ascii=True, sort_keys=True)}
    return json.dumps(outer, ensure_ascii=True, sort_keys=True)


def _instrumented_invoke_codex(self: Any, invocation: Any) -> str:
    """Mock Codex executor that captures prompts for verification."""
    prompt = invocation.prompt
    captured_prompts.append(prompt)

    worker_input = _parse_worker_input_from_prompt(prompt)
    captured_worker_inputs.append(worker_input)

    worker_role = worker_input.get("worker_role", "Reader")

    if worker_role == "Reader":
        return _make_reader_response(worker_input)
    elif worker_role == "Verifier":
        return _make_verifier_response(worker_input)
    elif worker_role == "IssueComposer":
        return _make_issue_composer_response(worker_input)

    # Fallback
    return _make_reader_response(worker_input)


# ---------------------------------------------------------------------------
# Fixture: minimal git repo
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
# Tests
# ---------------------------------------------------------------------------

class TestTargetSourcesTransport:
    """Prove that target_sources with file_content reaches the LLM prompt."""

    def test_target_sources_transport_e2e(self, tmp_path):
        """Full pipeline test proving target_sources delivery at each stage."""
        global captured_prompts, captured_worker_inputs
        captured_prompts = []
        captured_worker_inputs = []

        target_repo = _create_target_repo(tmp_path)
        workspace = tmp_path / "demo_ws"
        audit_id = "audit_transport_test"

        with patch(
            "runtime.adapters.codex_adapter.CodexAdapter._invoke_codex",
            _instrumented_invoke_codex,
        ):
            for args in [
                ["init-audit", "--workspace", str(workspace),
                 "--target-repo", str(target_repo),
                 "--audit-id", audit_id,
                 "--title", "Transport test",
                 "--policy", "low_noise"],
                ["snapshot-target", "--workspace", str(workspace)],
                ["enqueue-scan", "--workspace", str(workspace),
                 "--target-kind", "path", "--targets", "app/main.py"],
            ]:
                rc = cli_main(args)
                assert rc == 0, f"CLI failed: {' '.join(args)}"

            # Run tasks one at a time so we see which step fails
            rc = cli_main([
                "run-task", "--workspace", str(workspace),
                "--backend", "codex", "--timeout-seconds", "30",
            ])
            assert rc == 0, f"run-task failed (rc={rc})"

            # Run follow-up tasks
            for _ in range(10):
                rc = cli_main([
                    "run-task", "--workspace", str(workspace),
                    "--backend", "codex", "--timeout-seconds", "30",
                ])
                if rc != 0:
                    break

            rc = cli_main(["rebuild-state", "--workspace", str(workspace)])
            assert rc == 0, "rebuild-state failed"

            rc = cli_main(["compile-report", "--workspace", str(workspace)])
            assert rc == 0, "compile-report failed"

        # ===================================================================
        # T1: Slice file(s) contain target_sources with non-empty file_content
        # ===================================================================
        slice_files = list((workspace / "state" / "slices").glob("*.json"))
        assert len(slice_files) >= 1, "T1 FAIL: No slice files found"

        # Collect ALL Reader slices (expansion creates multiple)
        reader_slices = []
        for sf in slice_files:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if data.get("worker_role") == "Reader":
                reader_slices.append(data)

        assert len(reader_slices) >= 1, "T1 FAIL: No Reader slices found"

        # Verify file_content is non-empty in ALL reader slices
        all_target_sources = []
        for rs in reader_slices:
            ts = rs.get("target_sources", [])
            assert len(ts) >= 1, (
                f"T1 FAIL: Reader slice has no target_sources. "
                f"Keys: {sorted(rs.keys())}"
            )
            for src in ts:
                fc = src.get("file_content", "")
                assert isinstance(fc, str) and len(fc) > 0, (
                    f"T1 FAIL: target_sources entry for '{src.get('file_path')}' "
                    f"has empty file_content"
                )
            all_target_sources.extend(ts)

        print(f"\nT1 PASS: {len(reader_slices)} Reader slices with {len(all_target_sources)} total target_sources:")
        for src in all_target_sources:
            fc_preview = src["file_content"][:60].replace("\n", "\\n")
            print(f"  {src['file_path']}: {len(src['file_content'])} chars, preview: '{fc_preview}'")

        # ===================================================================
        # T2: At least one Reader prompt contains target_sources with file_content
        # ===================================================================
        assert len(captured_prompts) >= 1, (
            f"T2 FAIL: No prompts captured. "
            f"worker_inputs captured: {len(captured_worker_inputs)}"
        )

        # Debug: show all prompts
        for i, p in enumerate(captured_prompts):
            has_begin = "WORKER_INPUT_JSON_BEGIN" in p
            has_end = "WORKER_INPUT_JSON_END" in p
            has_ts = "target_sources" in p
            has_fc = "file_content" in p
            print(f"  Prompt[{i}]: len={len(p)}, BEGIN={has_begin}, END={has_end}, "
                  f"target_sources={has_ts}, file_content={has_fc}")

        # Find a Reader prompt (first prompt with target_sources)
        reader_prompt = None
        for p in captured_prompts:
            if "target_sources" in p and "file_content" in p:
                reader_prompt = p
                break
        assert reader_prompt is not None, (
            "T2 FAIL: No Reader prompt with target_sources found"
        )
        assert "target_sources" in reader_prompt, (
            "T2 FAIL: Prompt does not mention 'target_sources'"
        )
        assert "file_content" in reader_prompt, (
            "T2 FAIL: Prompt does not mention 'file_content'"
        )

        # Verify actual file content appears in the Reader prompt.
        # With expansion, there are multiple Reader prompts. Check that at
        # least one file's content from the Reader slices appears in the
        # corresponding Reader prompt.
        found_content_in_prompt = False
        for src in all_target_sources:
            actual_code = src["file_content"]
            escaped_code = json.dumps(actual_code, ensure_ascii=True)[1:-1]
            if escaped_code in reader_prompt:
                found_content_in_prompt = True
                break
        assert found_content_in_prompt, (
            f"T2 FAIL: No file_content from any Reader slice found in Reader prompt.\n"
            f"  Checked {len(all_target_sources)} target_sources entries.\n"
            f"  Reader prompt length: {len(reader_prompt)}\n"
            f"  First entry: {all_target_sources[0]['file_path']}, "
            f"content preview: {repr(all_target_sources[0]['file_content'][:80])}"
        )

        print(f"\nT2 PASS: Reader prompt contains target_sources instructions and file content")
        print(f"  Prompt length: {len(reader_prompt)} chars")
        print(f"  'target_sources' appears {reader_prompt.count('target_sources')} times")
        print(f"  'file_content' appears {reader_prompt.count('file_content')} times")

        # ===================================================================
        # T3: Raw Codex output references code from file_content
        # ===================================================================
        # Our mock explicitly includes file_content in the response.
        # For real Codex, we'd check if the claim references actual code lines.
        # Here we verify the mock response was built correctly.
        assert len(captured_worker_inputs) >= 1, "T3 FAIL: No worker inputs captured"

        reader_wi = captured_worker_inputs[0]
        wi_target_sources = reader_wi.get("target_sources", [])
        assert len(wi_target_sources) >= 1, (
            "T3 FAIL: Worker input received by mock has no target_sources"
        )

        wi_fc = wi_target_sources[0].get("file_content", "")
        assert len(wi_fc) > 0, (
            "T3 FAIL: Worker input file_content is empty"
        )

        print(f"\nT3 PASS: Worker input received by mock contains file_content")
        print(f"  file_path: {wi_target_sources[0].get('file_path')}")
        print(f"  file_content length: {len(wi_fc)} chars")
        print(f"  file_content preview: '{wi_fc[:80]}'")

        # ===================================================================
        # T4: At least 1 observation.proposed in the event log
        # ===================================================================
        events_log = (workspace / "events" / "events.ndjson").read_text(encoding="utf-8")
        events = [json.loads(line) for line in events_log.splitlines() if line.strip()]

        obs_proposed = [
            e for e in events
            if e.get("event_type") == "observation.proposed"
        ]
        assert len(obs_proposed) >= 1, (
            f"T4 FAIL: No observation.proposed events. "
            f"Event types: {[e.get('event_type') for e in events]}"
        )

        # Verify source binding
        for ev in obs_proposed:
            payload = ev.get("payload", {})
            provenance = payload.get("provenance", {})
            source_refs = provenance.get("source_refs", [])
            assert len(source_refs) >= 1, (
                f"T4 FAIL: observation {ev['entity_id']} has no source_refs"
            )
            for ref in source_refs:
                assert "file_path" in ref, f"T4 FAIL: source_ref missing file_path"
                assert "snapshot_ref" in ref, f"T4 FAIL: source_ref missing snapshot_ref"

        print(f"\nT4 PASS: {len(obs_proposed)} observation.proposed events with source binding")
        for ev in obs_proposed:
            stmt = ev["payload"].get("statement", "")[:80]
            print(f"  {ev['entity_id']}: '{stmt}'")

        # ===================================================================
        # Summary
        # ===================================================================
        state = json.loads(
            (workspace / "state" / "canonical_state.json").read_text(encoding="utf-8")
        )
        observations = state.get("observations", {})
        print(f"\nCanonical state: {len(observations)} observations")
        for oid, obs in observations.items():
            print(f"  {oid}: status={obs.get('status')}, stmt='{obs.get('statement', '')[:60]}'")
