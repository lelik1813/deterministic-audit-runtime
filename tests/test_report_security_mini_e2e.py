from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse

from cli import command_compile_report


def _write_workspace_config(workspace: Path, audit_id: str, target_repo_path: Path) -> None:
    payload = {
        "schema_version": "1.0.0",
        "audit_id": audit_id,
        "target_repo_path": str(target_repo_path),
        "title": "Mini security fixture",
        "policy": "low_noise",
    }
    (workspace / "audit_config.json").write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _build_canonical_state(audit_id: str) -> dict:
    return {
        "audit": {
            "id": audit_id,
            "status": "analyzed",
            "title": "Mini fixture test",
            "target": {"repo_label": "dar-fixture-mini", "repo_path": "C:/tmp/repo", "vcs": "git"},
            "current_snapshot_ref": "c1f981fa2bf9e7c91c32b4ba7cbec49af76eb13d",
        },
        "observations": {
            "obs_cors_wildcard": {
                "id": "obs_cors_wildcard",
                "audit_id": audit_id,
                "status": "verified",
                "statement": "CORS wildcard origins allow cross-origin requests from arbitrary domains.",
                "evidence_class": "direct_code_fact",
                "evidence_origin": "deterministic_pattern",
                "pattern_match_ids": ["pm_cors_wildcard_75f446faac3487a4"],
                "provenance": {
                    "source_refs": [
                        {
                            "file_path": "app.py",
                            "line_range": {"start": 26, "end": 26},
                            "snapshot_ref": "c1f981fa2bf9e7c91c32b4ba7cbec49af76eb13d",
                        }
                    ]
                },
            },
            "obs_jwt_no_verify": {
                "id": "obs_jwt_no_verify",
                "audit_id": audit_id,
                "status": "verified",
                "statement": "JWT decoding disables verify_signature and may accept forged tokens.",
                "evidence_class": "direct_code_fact",
                "evidence_origin": "deterministic_pattern",
                "pattern_match_ids": ["pm_jwt_no_verify_138441a180d340db"],
                "provenance": {
                    "source_refs": [
                        {
                            "file_path": "app.py",
                            "line_range": {"start": 89, "end": 89},
                            "snapshot_ref": "c1f981fa2bf9e7c91c32b4ba7cbec49af76eb13d",
                        }
                    ]
                },
            },
        },
        "issues": {},
        "questions": {},
        "contradictions": {},
        "decisions": {},
        "candidates": {},
    }


def test_compile_report_security_mini_fixture_e2e(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / "state").mkdir()
    (workspace / "reports").mkdir()

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True)
    audit_id = "audit_test_mini"

    schema_src = PROJECT_ROOT / "schema"
    if schema_src.exists():
        shutil.copytree(schema_src, workspace / "schema")
    config_src = PROJECT_ROOT / "config"
    if config_src.exists():
        shutil.copytree(config_src, workspace / "config")

    _write_workspace_config(workspace, audit_id=audit_id, target_repo_path=target_repo)
    canonical_state = _build_canonical_state(audit_id=audit_id)
    (workspace / "state" / "canonical_state.json").write_text(
        json.dumps(canonical_state, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    result = command_compile_report(argparse.Namespace(workspace=workspace, report_name=None))
    assert result["command"] == "compile-report"
    assert result["audit_id"] == audit_id

    report_path = workspace / "reports" / f"report.{audit_id}.json"
    assert report_path.exists(), "compiled report not written"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    fixture_path = Path(__file__).resolve().parent / "fixtures" / "golden_report_security_mini.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert report.get("audit", {}).get("status") == fixture["audit_status"]

    findings = report.get("findings", [])
    suppression_records = report.get("suppression_records", [])
    required_ids = set(fixture["required_observation_ids"])

    # Primary success criterion
    if len(findings) >= fixture["finding_count_min"]:
        found_ids: set[str] = set()
        has_high = False
        for finding in findings:
            for oid in finding.get("source_observation_ids", []):
                if isinstance(oid, str):
                    found_ids.add(oid)
            if finding.get("severity") == fixture["required_severity_at_least_one"]:
                has_high = True
        assert required_ids.issubset(found_ids), "not all required observations reached findings"
        assert has_high, "expected at least one high severity finding"
    else:
        # Fallback criterion: explicit suppression coverage
        suppressed_ids = {
            rec.get("observation_id")
            for rec in suppression_records
            if isinstance(rec, dict)
        }
        assert required_ids.issubset(suppressed_ids), (
            "findings below threshold and suppression coverage is incomplete"
        )
