"""Failure artifact persistence for debugging rejected candidate events.

When a backend run produces rejected candidates, this module persists
the raw LLM output, normalized candidates, and rejection diagnostics
to the workspace so they can be compared side-by-side.

Artifacts are stored under:  <workspace>/runs/failures/<run_id>/
  - raw_output.json          — exact text the backend returned
  - normalized_candidates.json — candidate events after normalization + enrichment
  - rejection_diagnostics.json — per-event rejection classification

Only written when at least one candidate was rejected.
Successful runs are NOT affected.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_failure_bundle(
    workspace_root: Path,
    *,
    run_id: str,
    task_id: str,
    raw_output: str | None,
    normalized_candidates: list[dict[str, Any]],
    event_outcomes: list[dict[str, Any]],
) -> Path | None:
    """Persist failure artifacts for a run with rejected candidates.

    Returns the failure directory path if written, None if skipped
    (no rejections or no data to persist).

    This is a bounded, debug-safe operation:
    - Only called when rejected_events > 0
    - Files are JSON, size-bounded to raw_output (no truncation of diagnostics)
    - Does not raise on write failure — logs warning instead
    """
    has_rejection = any(
        outcome.get("outcome") == "rejected"
        for outcome in event_outcomes
    )
    if not has_rejection:
        return None

    failure_dir = workspace_root / "runs" / "failures" / run_id
    failure_dir.mkdir(parents=True, exist_ok=True)

    # 1. Raw output
    if raw_output is not None:
        _write_json(failure_dir / "raw_output.json", {
            "run_id": run_id,
            "task_id": task_id,
            "raw_output": raw_output,
            "raw_output_length": len(raw_output),
        })

    # 2. Normalized candidates (post-enrichment, pre-validation)
    _write_json(failure_dir / "normalized_candidates.json", {
        "run_id": run_id,
        "task_id": task_id,
        "candidate_count": len(normalized_candidates),
        "candidates": normalized_candidates,
    })

    # 3. Rejection diagnostics — per-event breakdown
    rejected = [
        {
            "event_id": o.get("event_id"),
            "event_type": o.get("event_type"),
            "entity_type": o.get("entity_type"),
            "entity_id": o.get("entity_id"),
            "rejection": o.get("rejection"),
            "issue_codes": o.get("issue_codes", []),
        }
        for o in event_outcomes
        if o.get("outcome") == "rejected"
    ]
    accepted_count = sum(1 for o in event_outcomes if o.get("outcome") == "accepted")
    _write_json(failure_dir / "rejection_diagnostics.json", {
        "run_id": run_id,
        "task_id": task_id,
        "total_candidates": len(event_outcomes),
        "accepted_count": accepted_count,
        "rejected_count": len(rejected),
        "rejected_events": rejected,
    })

    return failure_dir


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically. Never raises — silently skips on failure."""
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=True, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass  # Failure artifact persistence must not break the pipeline
