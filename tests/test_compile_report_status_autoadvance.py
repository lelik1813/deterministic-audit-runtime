from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cli
from runtime.report_compiler import ReportCompileError


def test_compile_report_advances_initialized_status_before_retry(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    (workspace / "state").mkdir(parents=True)
    (workspace / "reports").mkdir(parents=True)

    (workspace / "audit_config.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "audit_id": "audit_status_retry",
                "target_repo_path": str(tmp_path / "repo"),
                "title": "Status retry",
                "policy": "low_noise",
            },
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (workspace / "state" / "canonical_state.json").write_text(
        json.dumps(
            {
                "audit": {
                    "id": "audit_status_retry",
                    "status": "initialized",
                    "target": {"repo_path": "C:/tmp/repo", "vcs": "git", "repo_label": "repo"},
                    "created_at": "2026-03-31T00:00:00Z",
                    "updated_at": "2026-03-31T00:00:00Z",
                    "current_snapshot_ref": "abc123",
                    "title": "Status retry",
                },
                "tasks": {},
                "observations": {},
                "hypotheses": {},
                "issues": {},
                "questions": {},
                "contradictions": {},
                "decisions": {},
                "candidates": {},
            },
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    calls = {"write_report": 0, "status_events": 0}

    class FakeCompiler:
        def __init__(self, _workspace_root: Path) -> None:
            pass

        def write_report(self, report_name: str | None = None):  # noqa: ANN001
            calls["write_report"] += 1
            if calls["write_report"] == 1:
                raise ReportCompileError(
                    "Cannot compile populated report while audit.status='initialized'. "
                    "Advance status to at least in_progress before report emission."
                )
            return SimpleNamespace(report_id="report_test", report_path=workspace / "reports" / "report.json")

    def _fake_process_candidate_events(root_dir, candidate_events, **kwargs):  # noqa: ANN001
        assert Path(root_dir).resolve() == workspace.resolve()
        assert kwargs.get("audit_id") == "audit_status_retry"
        assert len(candidate_events) == 1
        event = candidate_events[0]
        assert event["event_type"] == "audit.updated"
        assert event["payload"]["status"] == "in_progress"
        calls["status_events"] += 1
        return SimpleNamespace(accepted_events=1, rejected_events=0, projection_result=object())

    monkeypatch.setattr(cli, "ReportCompiler", FakeCompiler)
    monkeypatch.setattr(cli, "process_candidate_events", _fake_process_candidate_events)

    result = cli.command_compile_report(argparse.Namespace(workspace=workspace, report_name=None))
    assert result["command"] == "compile-report"
    assert result["audit_id"] == "audit_status_retry"
    assert calls["write_report"] == 2
    assert calls["status_events"] == 1
