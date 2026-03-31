from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.report_compiler import ReportCompileError, ReportCompiler


def _state(status: str) -> dict:
    return {
        "audit": {
            "id": "audit_status_guard",
            "status": status,
            "title": "Status guard test",
            "target": {"path": "/tmp/repo"},
            "current_snapshot_ref": "abc123",
        },
        "observations": {},
        "issues": {},
        "questions": {},
        "contradictions": {},
        "decisions": {},
        "candidates": {},
    }


def test_populated_report_rejected_when_initialized(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _state("initialized")
    state["observations"]["obs_001"] = {
        "id": "obs_001",
        "audit_id": "audit_status_guard",
        "status": "verified",
        "statement": "Verified fact exists.",
        "evidence_class": "direct_code_fact",
        "provenance": {"source_refs": [{"file_path": "app.py"}]},
    }

    with pytest.raises(ReportCompileError, match="audit.status='initialized'"):
        compiler.build_report(canonical_state=state)


def test_populated_report_allowed_when_in_progress(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _state("in_progress")
    state["observations"]["obs_001"] = {
        "id": "obs_001",
        "audit_id": "audit_status_guard",
        "status": "verified",
        "statement": "Verified fact exists.",
        "evidence_class": "direct_code_fact",
        "provenance": {"source_refs": [{"file_path": "app.py"}]},
    }
    report = compiler.build_report(canonical_state=state)
    assert report["audit"]["status"] == "in_progress"


def test_initialized_allowed_for_empty_report(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _state("initialized")
    report = compiler.build_report(canonical_state=state)
    assert report["audit"]["status"] == "initialized"
    assert report["findings"] == []


def test_unknown_audit_status_rejected(tmp_path: Path) -> None:
    compiler = ReportCompiler(tmp_path)
    state = _state("foo_bar_unknown")
    with pytest.raises(ReportCompileError, match="Unsupported audit.status"):
        compiler.build_report(canonical_state=state)
