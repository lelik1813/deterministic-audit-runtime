from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# BACKEND SELECTION: Import selector, NOT direct adapter
from runtime.adapters import (
    AdapterFactoryConfig,
    BackendKind,
    BackendSelectionConfig,
    BackendSelector,
    BackendUnavailableError,
    ClaudeSdkAdapterConfig,
    CodexAdapter,
    CodexAdapterError,
    select_and_create_adapter,
    set_default_selector,
)
from runtime.event_store import (
    WORKSPACE_LOCK_NAME,
    LockAcquisitionError,
    atomic_write_text,
    workspace_lock,
)
from runtime.failure_artifacts import write_failure_bundle
from runtime.policies import AuditPolicy, PolicyStore
from runtime.processing import (
    CandidateEventProcessingError,
    _trace_outcome,
    process_candidate_events,
    recover_runtime_state,
)
from runtime.report_compiler import ReportCompileError, ReportCompiler
from runtime.run_ledger import RunLedger, WorkerExecutionTraceContext
from runtime.slice_builder import MemorySliceBuilder
from runtime.snapshot import RepositorySnapshot, SnapshotError
from runtime.tasks import TASK_STATUSES, TASK_TYPES, TARGET_KINDS, TaskPlanner, TaskQueueStore


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_CONFIG_NAME = "audit_config.json"
WORKSPACE_CONFIG_SCHEMA_VERSION = "1.0.0"


def emit_progress(message: str) -> None:
    """Emit progress message to stderr for runtime visibility."""
    print(f"▶ {message}", file=sys.stderr, flush=True)


def emit_progress_detail(message: str) -> None:
    """Emit progress detail (indented) to stderr."""
    print(f"  └─ {message}", file=sys.stderr, flush=True)


class CliError(Exception):
    """Raised when a CLI command cannot complete successfully."""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = _run_command(args)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if result is not None:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap CLI for the external audit runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init-audit",
        help="Initialize a new audit workspace for an external repository.",
    )
    init_parser.add_argument("--workspace", type=Path, required=True)
    init_parser.add_argument("--target-repo", type=Path, required=True)
    init_parser.add_argument("--audit-id", required=True)
    init_parser.add_argument("--title", default=None)
    init_parser.add_argument(
        "--policy",
        choices=("strict_security", "low_noise", "exploratory"),
        default="low_noise",
        help="Audit policy profile (default: low_noise)",
    )
    init_parser.set_defaults(command_fn=command_init_audit)

    snapshot_parser = subparsers.add_parser(
        "snapshot-target",
        help="Capture and persist the current target repository snapshot.",
    )
    snapshot_parser.add_argument("--workspace", type=Path, required=True)
    snapshot_parser.set_defaults(command_fn=command_snapshot_target)

    enqueue_parser = subparsers.add_parser(
        "enqueue-scan",
        help="Create initial module_scan tasks for one or more repo targets.",
    )
    enqueue_parser.add_argument("--workspace", type=Path, required=True)
    enqueue_parser.add_argument("--targets", nargs="+", required=True)
    enqueue_parser.add_argument("--target-kind", choices=("path", "module"), default="path")
    enqueue_parser.set_defaults(command_fn=command_enqueue_scan)

    run_task_parser = subparsers.add_parser(
        "run-task",
        help="Claim the next runnable task and run it end-to-end.",
    )
    run_task_parser.add_argument("--workspace", type=Path, required=True)
    run_task_parser.add_argument("--model", default=None)
    run_task_parser.add_argument("--timeout-seconds", type=int, default=300)
    run_task_parser.add_argument(
        "--backend",
        choices=("codex", "claude"),
        default=None,
        help=(
            "Explicitly select backend. If not specified, uses default. "
            "CRITICAL: No implicit fallback - if Claude is unavailable, "
            "you must explicitly request --backend codex."
        ),
    )
    run_task_parser.set_defaults(command_fn=command_run_task)

    rebuild_parser = subparsers.add_parser(
        "rebuild-state",
        help="Rebuild canonical state from the accepted event log.",
    )
    rebuild_parser.add_argument("--workspace", type=Path, required=True)
    rebuild_parser.set_defaults(command_fn=command_rebuild_state)

    report_parser = subparsers.add_parser(
        "compile-report",
        help="Compile a report from accepted canonical state.",
    )
    report_parser.add_argument("--workspace", type=Path, required=True)
    report_parser.add_argument("--report-name", default=None)
    report_parser.set_defaults(command_fn=command_compile_report)

    run_all_parser = subparsers.add_parser(
        "run-all-tasks",
        help="Run all pending tasks in a loop until queue is empty or limit reached.",
    )
    run_all_parser.add_argument("--workspace", type=Path, required=True)
    run_all_parser.add_argument("--backend", choices=("codex", "claude"), default=None)
    run_all_parser.add_argument("--model", default=None)
    run_all_parser.add_argument("--timeout-seconds", type=int, default=300)
    run_all_parser.add_argument(
        "--max-iterations", type=int, default=50,
        help="Maximum number of task iterations before stopping.",
    )
    run_all_parser.set_defaults(command_fn=command_run_all_tasks)

    list_tasks_parser = subparsers.add_parser(
        "list-tasks",
        help="List tasks from the task queue with optional filtering.",
    )
    list_tasks_parser.add_argument("--workspace", type=Path, required=True)
    list_tasks_parser.add_argument("--status", choices=TASK_STATUSES, default=None, help="Filter by task status")
    list_tasks_parser.add_argument("--type", dest="task_type", choices=TASK_TYPES, default=None, help="Filter by task type")
    list_tasks_parser.add_argument("--target-kind", choices=TARGET_KINDS, default=None, help="Filter by target kind")
    list_tasks_parser.add_argument("--limit", type=int, default=None, help="Limit number of results")
    list_tasks_parser.add_argument("--sort-by", choices=("created_at", "updated_at"), default="created_at", help="Sort field")
    list_tasks_parser.add_argument("--order", choices=("asc", "desc"), default="asc", help="Sort order")
    list_tasks_parser.set_defaults(command_fn=command_list_tasks)

    return parser


def _run_command(args: argparse.Namespace) -> Any:
    workspace = getattr(args, "workspace", None)
    if workspace is None:
        return args.command_fn(args)

    workspace_root = workspace.expanduser().resolve()
    args.workspace = workspace_root
    try:
        with workspace_lock(workspace_root, owner=f"cli.{args.command}"):
            if args.command != "init-audit":
                _recover_workspace_runtime(workspace_root, command_name=args.command)
            return args.command_fn(args)
    except LockAcquisitionError as exc:
        raise CliError(f"Workspace is already in use and cannot be locked safely: {exc}") from exc
    except CandidateEventProcessingError as exc:
        raise CliError(str(exc)) from exc


def command_init_audit(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = args.workspace.expanduser().resolve()
    target_repo_root = resolve_target_repo_root(args.target_repo)
    audit_id = validate_audit_id(args.audit_id)
    title = args.title or f"Audit {audit_id}"
    policy = args.policy or "low_noise"

    ensure_empty_workspace(workspace_root)
    prepare_workspace_directories(workspace_root)

    # Copy config directory with policies.yaml
    config_src = PROJECT_ROOT / "config"
    if config_src.exists():
        shutil.copytree(config_src, workspace_root / "config")

    write_workspace_config(
        workspace_root,
        {
            "schema_version": WORKSPACE_CONFIG_SCHEMA_VERSION,
            "audit_id": audit_id,
            "target_repo_path": str(target_repo_root),
            "title": title,
            "policy": policy,
        },
    )

    TaskQueueStore(workspace_root)
    result = process_candidate_events(
        workspace_root,
        [build_audit_created_event(audit_id=audit_id, target_repo_root=target_repo_root, title=title)],
        audit_id=audit_id,
    )
    if result.accepted_events != 1 or result.rejected_events != 0 or result.projection_result is None:
        raise CliError(f"init-audit failed to initialize accepted audit state: {result}")

    return {
        "command": "init-audit",
        "workspace": str(workspace_root),
        "target_repo_root": str(target_repo_root),
        "audit_id": audit_id,
        "policy": policy,
        "canonical_state_path": str(result.projection_result.canonical_state_path),
        "projection_id": result.projection_result.projection_id,
    }


def command_snapshot_target(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = args.workspace.expanduser().resolve()
    config = load_workspace_config(workspace_root)
    audit = load_current_audit(workspace_root)
    snapshot = capture_clean_snapshot(Path(config["target_repo_path"]))

    current_snapshot_ref = audit.get("current_snapshot_ref")
    if current_snapshot_ref == snapshot.snapshot_ref:
        return {
            "command": "snapshot-target",
            "workspace": str(workspace_root),
            "audit_id": config["audit_id"],
            "snapshot_ref": snapshot.snapshot_ref,
            "outcome": "unchanged",
        }

    result = process_candidate_events(
        workspace_root,
        [
            build_audit_updated_event(
                audit=audit,
                snapshot_ref=snapshot.snapshot_ref,
                actor_id="cli.snapshot-target",
            )
        ],
        audit_id=config["audit_id"],
    )
    if result.accepted_events != 1 or result.rejected_events != 0 or result.projection_result is None:
        raise CliError(f"snapshot-target failed to persist the captured snapshot_ref: {result}")

    return {
        "command": "snapshot-target",
        "workspace": str(workspace_root),
        "audit_id": config["audit_id"],
        "snapshot_ref": snapshot.snapshot_ref,
        "outcome": "updated",
        "canonical_state_path": str(result.projection_result.canonical_state_path),
        "projection_id": result.projection_result.projection_id,
    }


def command_enqueue_scan(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = args.workspace.expanduser().resolve()
    config = load_workspace_config(workspace_root)
    snapshot_ref = load_current_snapshot_ref(workspace_root)
    planner = TaskPlanner(workspace_root)
    results = planner.enqueue_initial_scan_tasks(
        config["audit_id"],
        args.targets,
        snapshot_ref,
        target_kind=args.target_kind,
    )
    return {
        "command": "enqueue-scan",
        "workspace": str(workspace_root),
        "audit_id": config["audit_id"],
        "snapshot_ref": snapshot_ref,
        "target_kind": args.target_kind,
        "results": [
            {
                "outcome": result.outcome,
                "task_id": result.task.id,
                "task_type": result.task.type,
                "target": result.task.target.to_dict(),
            }
            for result in results
        ],
    }


def command_run_task(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = args.workspace.expanduser().resolve()
    config = load_workspace_config(workspace_root)

    emit_progress("Planning follow-up tasks...")
    planner = TaskPlanner(workspace_root)
    planner.enqueue_follow_up_tasks(config["audit_id"])

    emit_progress("Selecting task...")
    queue = TaskQueueStore(workspace_root)
    task, task_acquisition = select_task_for_execution(queue, audit_id=config["audit_id"])
    if task is None:
        emit_progress("No runnable tasks available")
        return {
            "command": "run-task",
            "workspace": str(workspace_root),
            "audit_id": config["audit_id"],
            "outcome": "no_runnable_task",
        }

    target_value = task.target.value if len(task.target.value) <= 50 else task.target.value[:47] + "..."
    emit_progress(f"Task claimed: {task.id} ({task.type}) -> {target_value}")

    emit_progress("Binding snapshot...")
    target_repo_root = Path(config["target_repo_path"]).expanduser().resolve()
    snapshot = bind_task_snapshot(target_repo_root, task.target.snapshot_ref)
    run_ledger = RunLedger(workspace_root)
    slice_builder = MemorySliceBuilder(workspace_root)

    # BACKEND SELECTION: Use selector, not direct instantiation
    # This is THE ONLY PLACE where the backend adapter is created for run-task.
    # Default to Claude SDK if no explicit backend specified
    if args.backend == "codex":
        explicit_backend = BackendKind.CODEX
    elif args.backend == "claude":
        explicit_backend = BackendKind.CLAUDE_SDK
    else:
        # No --backend specified: default to Claude SDK
        explicit_backend = BackendKind.CLAUDE_SDK

    backend_name = "codex" if explicit_backend == BackendKind.CODEX else "claude_sdk"
    emit_progress(f"Backend: {backend_name}")

    # Build adapter config
    adapter_config = AdapterFactoryConfig(
        workspace_root=str(workspace_root),
        invocation_dir=str(target_repo_root),
        model=args.model,
        timeout_seconds=args.timeout_seconds,
    )

    # Add Claude SDK config if using Claude
    if explicit_backend == BackendKind.CLAUDE_SDK:
        adapter_config = AdapterFactoryConfig(
            workspace_root=str(workspace_root),
            invocation_dir=str(target_repo_root),
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            claude_sdk_config=ClaudeSdkAdapterConfig(
                working_directory=str(target_repo_root),
                default_timeout_seconds=args.timeout_seconds,
            ),
        )

    try:
        backend_kind, adapter = select_and_create_adapter(
            explicit_backend=explicit_backend,
            config=adapter_config,
        )
    except BackendUnavailableError as exc:
        raise CliError(
            f"Backend '{args.backend or 'default'}' unavailable: {exc.reason}. "
            f"Available backends: {[b.value for b in exc.available_backends]}. "
            f"Use --backend codex to explicitly use Codex."
        )

    run_start = run_ledger.start_run(
        audit_id=config["audit_id"],
        snapshot_ref=snapshot.snapshot_ref,
        metadata={
            "entrypoint": "cli.run-task",
            "workspace": str(workspace_root),
            "target_repo_root": str(target_repo_root),
        },
    )

    slice_result = None
    slice_payload = None
    processing_result = None
    try:
        emit_progress("Building memory slice...")
        slice_result = slice_builder.write_slice(task.id, task=task, snapshot=snapshot)
        slice_payload = load_json(slice_result.slice_path)

        # Precondition: module_scan tasks MUST have target_sources with file content.
        # If absent, this is an infrastructure defect — do NOT invoke the backend.
        if (
            task.type == "module_scan"
            and task.target.kind in {"path", "module"}
            and not slice_payload.get("target_sources")
        ):
            diagnostic_reason = (
                f"SLICE_COMPLETENESS_VIOLATION: module_scan task '{task.id}' "
                f"has no target_sources. "
                f"target_paths={slice_payload.get('target_paths', [])}, "
                f"snapshot_ref={snapshot.snapshot_ref}. "
                f"Classified as INFRASTRUCTURE_DEFECT: slice builder could not "
                f"populate file content from snapshot."
            )
            emit_progress(f"PRECONDITION FAILED: {diagnostic_reason}")
            run_ledger.record_worker_execution_failure(
                trace_context=WorkerExecutionTraceContext(
                    run_id=run_start.run_id,
                    audit_id=config["audit_id"],
                    task_id=task.id,
                    slice_id=slice_result.slice_id,
                    worker_role=slice_payload["worker_role"],
                    adapter_invocation={"precondition": "slice_completeness_violation"},
                    input_digest=slice_result.slice_fingerprint,
                    output_digest=None,
                    slice_fingerprint=slice_result.slice_fingerprint,
                    snapshot_ref=snapshot.snapshot_ref,
                ),
                failure_stage="precondition_check",
                error_message=diagnostic_reason,
            )
            raise CliError(diagnostic_reason)

        emit_progress(f"Running backend (timeout: {args.timeout_seconds}s)...")
        backend_start = time.time()
        run_result = adapter.run_with_result(slice_payload["worker_role"], slice_result.slice_path)
        backend_elapsed = time.time() - backend_start
        emit_progress_detail(f"{len(run_result.candidate_events)} candidates returned in {backend_elapsed:.1f}s")

        emit_progress("Processing candidate events...")
        candidate_events = enrich_candidate_events(run_result.candidate_events, snapshot)
        processing_result = process_candidate_events(
            workspace_root,
            candidate_events,
            audit_id=config["audit_id"],
            trace_context=WorkerExecutionTraceContext(
                run_id=run_start.run_id,
                audit_id=config["audit_id"],
                task_id=task.id,
                slice_id=slice_result.slice_id,
                worker_role=slice_payload["worker_role"],
                adapter_invocation=run_result.invocation_metadata,
                input_digest=run_result.input_digest,
                output_digest=run_result.output_digest,
                slice_fingerprint=slice_result.slice_fingerprint,
                snapshot_ref=snapshot.snapshot_ref,
                prompt_digest=run_result.prompt_digest,
                raw_output_digest=run_result.raw_output_digest,
            ),
        )
        emit_progress_detail(
            f"{processing_result.accepted_events} accepted, {processing_result.rejected_events} rejected"
        )
        if processing_result.rejected_events > 0:
            for evt_outcome in processing_result.event_outcomes:
                if evt_outcome.outcome == "rejected":
                    codes = [issue.code for issue in evt_outcome.issues]
                    code = codes[0] if codes else "unknown"
                    msg = evt_outcome.issues[0].message[:120] if evt_outcome.issues else ""
                    emit_progress_detail(
                        f"  REJECTED {evt_outcome.event_type or '?'} [{code}] {msg}"
                    )

            failure_dir = write_failure_bundle(
                workspace_root,
                run_id=run_start.run_id,
                task_id=task.id,
                raw_output=getattr(run_result, "raw_output", None),
                normalized_candidates=candidate_events,
                event_outcomes=[_trace_outcome(o) for o in processing_result.event_outcomes],
            )
            if failure_dir is not None:
                emit_progress_detail(f"Failure artifacts: {failure_dir}")

        if processing_result.accepted_events == 0:
            if task_acquisition == "resumed_running" and is_duplicate_only_result(processing_result):
                pass
            else:
                raise CliError(
                    f"run-task produced no accepted candidate events for task '{task.id}'."
                )

        emit_progress("Completing task...")
        completed_task = queue.transition_task(task.id, "done")
    except Exception as exc:
        if isinstance(exc, CodexAdapterError) and slice_result is not None and slice_payload is not None:
            run_ledger.record_worker_execution_failure(
                trace_context=WorkerExecutionTraceContext(
                    run_id=run_start.run_id,
                    audit_id=config["audit_id"],
                    task_id=task.id,
                    slice_id=slice_result.slice_id,
                    worker_role=slice_payload["worker_role"],
                    adapter_invocation=getattr(exc, "invocation_metadata", {}) or {},
                    input_digest=getattr(exc, "input_digest", None),
                    output_digest=getattr(exc, "output_digest", None),
                    slice_fingerprint=slice_result.slice_fingerprint,
                    snapshot_ref=snapshot.snapshot_ref,
                    prompt_digest=getattr(exc, "prompt_digest", None),
                    raw_output_digest=getattr(exc, "raw_output_digest", None),
                ),
                failure_stage=str(getattr(exc, "failure_stage", None) or "adapter_failure"),
                error_message=str(exc),
            )
        queue.transition_task(task.id, "failed", error=str(exc))
        if isinstance(exc, CliError):
            raise
        raise CliError(f"run-task failed for task '{task.id}': {exc}") from exc

    follow_up_results: list[Any] = []
    if task.type != "compose_issue" and processing_result.accepted_events > 0:
        emit_progress("Enqueueing follow-up tasks...")
        canonical_state = load_json(workspace_root / "state" / "canonical_state.json")
        follow_up_results = planner.enqueue_follow_up_tasks(
            config["audit_id"],
            canonical_state,
        )
        enqueued_count = sum(1 for r in follow_up_results if r.outcome == "enqueued")
        if follow_up_results:
            emit_progress_detail(
                f"{len(follow_up_results)} follow-up results "
                f"({enqueued_count} enqueued)"
            )
    else:
        enqueued_count = 0

    # Expansion: if no new tasks were enqueued, scan more files from the snapshot.
    # This runs after ANY task type (including compose_issue) so the pipeline
    # continues to cover more files after finishing work on the current target.
    if processing_result.accepted_events > 0 and enqueued_count == 0:
        expansion_results = _expand_scan_coverage(
            workspace_root=workspace_root,
            audit_id=config["audit_id"],
            snapshot=snapshot,
            planner=planner,
            queue=queue,
        )
        follow_up_results.extend(expansion_results)
        if expansion_results:
            enqueued_expansion = sum(1 for r in expansion_results if r.outcome == "enqueued")
            emit_progress_detail(
                f"{enqueued_expansion} new module_scan tasks from expansion"
            )

    emit_progress("Done.")
    return {
        "command": "run-task",
        "workspace": str(workspace_root),
        "audit_id": config["audit_id"],
        "run_id": run_start.run_id,
        "run_ledger_path": str(run_ledger.ledger_path),
        "task": completed_task.to_dict(),
        "task_acquisition": task_acquisition,
        "worker_role": slice_payload["worker_role"],
        "slice_id": slice_result.slice_id,
        "slice_path": str(slice_result.slice_path),
        "trace_entry_id": processing_result.trace_entry_id,
        "candidate_event_types": [event["event_type"] for event in candidate_events],
        "accepted_events": processing_result.accepted_events,
        "rejected_events": processing_result.rejected_events,
        "event_outcomes": [
            {
                "event_type": outcome.event_type,
                "outcome": outcome.outcome,
                "issue_codes": [issue.code for issue in outcome.issues],
            }
            for outcome in processing_result.event_outcomes
        ],
        "follow_up_tasks": [
            {
                "outcome": result.outcome,
                "task_id": result.task.id,
                "task_type": result.task.type,
                "target": result.task.target.to_dict(),
            }
            for result in follow_up_results
        ],
    }


def command_run_all_tasks(args: argparse.Namespace) -> dict[str, Any]:
    """Run all pending tasks in a loop until the queue is empty or a limit is reached.

    Each iteration claims one pending task, executes it, and enqueues follow-up
    tasks (verify_claim → compose_issue).  The loop stops when:
      - no more runnable tasks exist
      - a task fails
      - ``--max-iterations`` is reached
    """
    max_iterations = getattr(args, "max_iterations", 50) or 50
    results: list[dict[str, Any]] = []
    for _ in range(max_iterations):
        try:
            result = command_run_task(args)
        except CliError:
            break
        if result.get("outcome") == "no_runnable_task":
            break
        results.append(result)
    return {
        "command": "run-all-tasks",
        "workspace": str(args.workspace),
        "iterations": len(results),
        "total_accepted_events": sum(r.get("accepted_events", 0) for r in results),
        "total_rejected_events": sum(r.get("rejected_events", 0) for r in results),
        "results": results,
    }


def command_rebuild_state(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = args.workspace.expanduser().resolve()
    config = load_workspace_config(workspace_root)
    result = recover_runtime_state(
        workspace_root,
        audit_id=config["audit_id"],
        lock_workspace=False,
    )
    return {
        "command": "rebuild-state",
        "workspace": str(workspace_root),
        "audit_id": config["audit_id"],
        "projection_id": result.projection_id,
        "total_events": result.total_events,
        "accepted_events": result.accepted_events,
        "canonical_state_path": str(result.canonical_state_path),
        "snapshot_path": str(result.snapshot_path),
    }


def command_compile_report(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = args.workspace.expanduser().resolve()
    config = load_workspace_config(workspace_root)
    compiler = ReportCompiler(workspace_root)
    try:
        result = compiler.write_report(report_name=args.report_name)
    except ReportCompileError as exc:
        if "audit.status='initialized'" not in str(exc):
            raise
        _advance_audit_to_in_progress_for_report(
            workspace_root=workspace_root,
            audit_id=config["audit_id"],
        )
        result = compiler.write_report(report_name=args.report_name)
    return {
        "command": "compile-report",
        "workspace": str(workspace_root),
        "audit_id": config["audit_id"],
        "report_id": result.report_id,
        "report_path": str(result.report_path),
    }


def command_list_tasks(args: argparse.Namespace) -> dict[str, Any]:
    """List tasks from the queue with optional filtering. Read-only operation."""
    workspace_root = args.workspace.expanduser().resolve()
    config = load_workspace_config(workspace_root)
    queue = TaskQueueStore(workspace_root)

    # Get all tasks for this audit (needed for counts)
    all_tasks = queue.list_tasks(audit_id=config["audit_id"])

    # Apply filters
    filtered_tasks = all_tasks
    if args.status:
        filtered_tasks = [t for t in filtered_tasks if t.status == args.status]
    if args.task_type:
        filtered_tasks = [t for t in filtered_tasks if t.type == args.task_type]
    if args.target_kind:
        filtered_tasks = [t for t in filtered_tasks if t.target.kind == args.target_kind]

    # Sort
    reverse = args.order == "desc"
    sort_field = args.sort_by
    filtered_tasks = sorted(
        filtered_tasks,
        key=lambda t: getattr(t, sort_field),
        reverse=reverse,
    )

    total_count = len(all_tasks)
    filtered_count = len(filtered_tasks)

    # Apply limit
    if args.limit is not None and args.limit > 0:
        filtered_tasks = filtered_tasks[:args.limit]

    # Compute status counts
    status_counts: dict[str, int] = {}
    for status in TASK_STATUSES:
        status_counts[status] = sum(1 for t in all_tasks if t.status == status)

    return {
        "command": "list-tasks",
        "workspace": str(workspace_root),
        "audit_id": config["audit_id"],
        "total": total_count,
        "filtered": filtered_count,
        "counts": status_counts,
        "tasks": [
            {
                "id": task.id,
                "type": task.type,
                "status": task.status,
                "target": {
                    "kind": task.target.kind,
                    "value": task.target.value,
                },
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
            for task in filtered_tasks
        ],
    }


def prepare_workspace_directories(workspace_root: Path) -> None:
    workspace_root.mkdir(parents=True, exist_ok=True)
    for directory_name in ("schema", "rules", "prompts"):
        shutil.copytree(PROJECT_ROOT / directory_name, workspace_root / directory_name)
    (workspace_root / "events").mkdir()
    (workspace_root / "state").mkdir()
    (workspace_root / "reports").mkdir()


def ensure_empty_workspace(workspace_root: Path) -> None:
    if workspace_root.exists() and any(
        entry for entry in workspace_root.iterdir() if entry.name != WORKSPACE_LOCK_NAME
    ):
        raise CliError(f"Workspace must be empty before init-audit: {workspace_root}")


def resolve_target_repo_root(target_repo_path: Path) -> Path:
    try:
        return RepositorySnapshot._resolve_repo_root(target_repo_path.expanduser().resolve())
    except SnapshotError as exc:
        raise CliError(f"Target repository is not a valid git repository: {target_repo_path}") from exc


def validate_audit_id(audit_id: str) -> str:
    if not isinstance(audit_id, str) or not audit_id.startswith("audit_"):
        raise CliError("audit_id must start with 'audit_'.")
    return audit_id


def write_workspace_config(workspace_root: Path, payload: dict[str, Any]) -> Path:
    path = workspace_root / WORKSPACE_CONFIG_NAME
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
    )
    return path


def load_workspace_config(workspace_root: Path) -> dict[str, Any]:
    config_path = workspace_root / WORKSPACE_CONFIG_NAME
    if not config_path.exists():
        raise CliError(f"Workspace config does not exist: {config_path}")
    config = load_json(config_path)
    required_keys = {"schema_version", "audit_id", "target_repo_path", "title"}
    allowed_keys = required_keys | {"policy"}
    config_keys = set(config.keys())
    if not required_keys.issubset(config_keys):
        missing = sorted(required_keys - config_keys)
        raise CliError(f"Workspace config is missing required keys: {', '.join(missing)}.")
    if not config_keys.issubset(allowed_keys):
        extra = sorted(config_keys - allowed_keys)
        raise CliError(f"Workspace config contains unsupported keys: {', '.join(extra)}.")
    if config["schema_version"] != WORKSPACE_CONFIG_SCHEMA_VERSION:
        raise CliError(
            f"Unsupported workspace config schema version '{config['schema_version']}'."
        )
    return config


def load_current_audit(workspace_root: Path) -> dict[str, Any]:
    state = load_json(workspace_root / "state" / "canonical_state.json")
    audit = state.get("audit")
    if not isinstance(audit, dict):
        raise CliError("Canonical state does not contain an accepted audit root.")
    return audit


def load_current_snapshot_ref(workspace_root: Path) -> str:
    audit = load_current_audit(workspace_root)
    snapshot_ref = audit.get("current_snapshot_ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref:
        raise CliError("Current audit does not have a captured snapshot_ref. Run snapshot-target first.")
    return snapshot_ref


def bind_task_snapshot(target_repo_root: Path, expected_snapshot_ref: str) -> RepositorySnapshot:
    current_snapshot = capture_clean_snapshot(target_repo_root)
    if current_snapshot.snapshot_ref != expected_snapshot_ref:
        raise CliError(
            "Target repository HEAD does not match the task snapshot_ref. "
            f"expected={expected_snapshot_ref} actual={current_snapshot.snapshot_ref}"
        )
    return current_snapshot


def capture_clean_snapshot(target_repo_root: Path) -> RepositorySnapshot:
    snapshot = RepositorySnapshot.capture(target_repo_root)
    status_output = RepositorySnapshot._git(snapshot.repo_root, "status", "--porcelain")
    if status_output.strip():
        raise CliError(
            "Target repository working tree must be clean before snapshot-target or run-task."
        )
    return snapshot


def build_audit_created_event(
    *,
    audit_id: str,
    target_repo_root: Path,
    title: str,
) -> dict[str, Any]:
    occurred_at = utc_now()
    return {
        "schema_version": "1.0.0",
        "id": build_event_id("audit.created", {"audit_id": audit_id, "target_repo_root": str(target_repo_root)}),
        "audit_id": audit_id,
        "entity_type": "audit",
        "entity_id": audit_id,
        "event_type": "audit.created",
        "occurred_at": occurred_at,
        "actor": {
            "actor_type": "system",
            "actor_id": "cli.init-audit",
            "role": None,
        },
        "snapshot_ref": None,
        "idempotency_key": f"{audit_id}:audit.created",
        "caused_by_event_id": None,
        "payload": {
            "id": audit_id,
            "status": "initialized",
            "target": {
                "repo_path": str(target_repo_root),
                "vcs": "git",
                "repo_label": target_repo_root.name,
            },
            "created_at": occurred_at,
            "updated_at": occurred_at,
            "current_snapshot_ref": None,
            "title": title,
        },
        "acceptance": pending_acceptance(),
    }


def build_audit_updated_event(
    *,
    audit: dict[str, Any],
    snapshot_ref: str | None,
    actor_id: str,
    new_status: str | None = None,
) -> dict[str, Any]:
    occurred_at = utc_now()
    updated_payload = json.loads(json.dumps(audit))
    updated_payload["updated_at"] = occurred_at
    if new_status is not None:
        updated_payload["status"] = new_status
    updated_payload["current_snapshot_ref"] = snapshot_ref
    snapshot_ref_token = snapshot_ref if snapshot_ref is not None else "null"
    return {
        "schema_version": "1.0.0",
        "id": build_event_id(
            "audit.updated",
            {
                "audit_id": audit["id"],
                "snapshot_ref": snapshot_ref,
                "status": updated_payload["status"],
            },
        ),
        "audit_id": audit["id"],
        "entity_type": "audit",
        "entity_id": audit["id"],
        "event_type": "audit.updated",
        "occurred_at": occurred_at,
        "actor": {
            "actor_type": "system",
            "actor_id": actor_id,
            "role": None,
        },
        "snapshot_ref": snapshot_ref,
        "idempotency_key": f"{audit['id']}:audit.updated:{snapshot_ref_token}:{updated_payload['status']}",
        "caused_by_event_id": None,
        "payload": updated_payload,
        "acceptance": pending_acceptance(),
    }


def _advance_audit_to_in_progress_for_report(*, workspace_root: Path, audit_id: str) -> None:
    audit = load_current_audit(workspace_root)
    status = audit.get("status")
    if status != "initialized":
        return

    event = build_audit_updated_event(
        audit=audit,
        snapshot_ref=audit.get("current_snapshot_ref"),
        actor_id="cli.compile-report",
        new_status="in_progress",
    )
    result = process_candidate_events(workspace_root, [event], audit_id=audit_id)
    if result.accepted_events != 1 or result.rejected_events != 0 or result.projection_result is None:
        raise CliError(
            "compile-report failed to advance audit status from initialized to in_progress."
        )


def enrich_candidate_events(
    candidate_events: list[dict[str, Any]],
    snapshot: RepositorySnapshot,
) -> list[dict[str, Any]]:
    enriched_events = json.loads(json.dumps(candidate_events))
    for event in enriched_events:
        for source_ref in iter_event_source_refs(event):
            file_path = source_ref.get("file_path")
            line_range = source_ref.get("line_range")
            if not isinstance(file_path, str) or not isinstance(line_range, dict):
                continue
            start_line = line_range.get("start")
            end_line = line_range.get("end")
            if not isinstance(start_line, int) or not isinstance(end_line, int):
                continue
            # Clamp line range to actual file length to prevent
            # SnapshotLineRangeError when the LLM overestimates.
            try:
                file_text = snapshot.read_text(file_path)
                actual_line_count = len(file_text.splitlines()) if isinstance(file_text, str) else 0
            except Exception:
                actual_line_count = 0
            if actual_line_count > 0:
                start_line = min(start_line, actual_line_count)
                end_line = min(end_line, actual_line_count)
            source_ref.update(
                snapshot.build_source_reference(
                    file_path,
                    start_line,
                    end_line,
                    include_file_hash=True,
                )
            )
    return enriched_events


def iter_event_source_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return []

    entity_type = event.get("entity_type")
    if entity_type == "observation":
        provenance = payload.get("provenance")
        if isinstance(provenance, dict) and isinstance(provenance.get("source_refs"), list):
            return provenance["source_refs"]
        return []

    if entity_type == "hypothesis" and isinstance(payload.get("supporting_source_refs"), list):
        return payload["supporting_source_refs"]

    if entity_type in {"contradiction", "decision"} and isinstance(payload.get("source_refs"), list):
        return payload["source_refs"]

    return []


def pending_acceptance() -> dict[str, Any]:
    return {
        "status": "pending",
        "decided_at": None,
        "decided_by": None,
        "reason": None,
    }


def build_event_id(event_type: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()[:16]
    event_type_token = event_type.replace(".", "_")
    return f"event_{event_type_token}_{digest}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _expand_scan_coverage(
    workspace_root: Path,
    audit_id: str,
    snapshot: RepositorySnapshot,
    planner: TaskPlanner,
    queue: TaskQueueStore,
    *,
    max_new_tasks: int = 10,
) -> list[Any]:
    """Create module_scan tasks for repo files that haven't been scanned yet.

    Called when the pipeline has accepted events but the follow-up planner
    produced no new enqueued tasks (verify_claim / compose_issue). This
    expands coverage to the next batch of unscanned files.
    """
    all_tracked = snapshot.list_tracked_files()
    if not all_tracked:
        return []

    # Determine which file paths already have tasks (any status)
    existing_tasks = queue.list_tasks(audit_id=audit_id)
    scanned_paths: set[str] = set()
    for t in existing_tasks:
        if t.type == "module_scan" and t.target.kind in ("path", "module"):
            normalized = t.target.value.replace("\\", "/")
            scanned_paths.add(normalized)

    # Filter out already-scanned files and non-code files
    unscanned = [
        f for f in all_tracked
        if f.replace("\\", "/") not in scanned_paths
        and not _is_skip_path(f)
    ]
    if not unscanned:
        return []

    # Limit batch size
    to_scan = unscanned[:max_new_tasks]
    return planner.enqueue_initial_scan_tasks(
        audit_id=audit_id,
        targets=to_scan,
        snapshot_ref=snapshot.snapshot_ref,
        target_kind="path",
    )


_SKIP_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".o", ".a", ".class",
    ".db", ".sqlite", ".lock",
})

_SKIP_PREFIXES = (
    ".git/",
    "node_modules/",
    "__pycache__/",
    ".tox/",
    ".mypy_cache/",
    ".pytest_cache/",
    "vendor/",
    ".venv/",
    "venv/",
    ".env/",
    "dist/",
    "build/",
)


def _is_skip_path(file_path: str) -> bool:
    """Return True for binary/generated/dependency paths that shouldn't be scanned."""
    normalized = file_path.replace("\\", "/")
    for prefix in _SKIP_PREFIXES:
        if normalized.startswith(prefix):
            return True
    dot = normalized.rfind(".")
    if dot >= 0:
        ext = normalized[dot:].lower()
        if ext in _SKIP_EXTENSIONS:
            return True
    return False


def _recover_workspace_runtime(workspace_root: Path, *, command_name: str) -> None:
    config = load_workspace_config(workspace_root)
    try:
        recover_runtime_state(
            workspace_root,
            audit_id=config["audit_id"],
            lock_workspace=False,
        )
    except CandidateEventProcessingError as exc:
        raise CliError(f"Failed to recover canonical state before '{command_name}': {exc}") from exc


def select_task_for_execution(
    queue: TaskQueueStore,
    *,
    audit_id: str,
) -> tuple[Any, str]:
    running_tasks = queue.list_tasks(audit_id=audit_id, status="running")
    if running_tasks:
        return running_tasks[0], "resumed_running"

    claimed = queue.claim_next_task(audit_id=audit_id)
    if claimed is None:
        return None, "none"
    return claimed, "claimed_pending"


def is_duplicate_only_result(result: Any) -> bool:
    if getattr(result, "accepted_events", 0) != 0 or getattr(result, "rejected_events", 0) <= 0:
        return False

    allowed_issue_codes = {"duplicate_submission", "append_not_persisted"}
    for outcome in getattr(result, "event_outcomes", ()):
        issue_codes = {issue.code for issue in outcome.issues}
        if not issue_codes or not issue_codes.issubset(allowed_issue_codes):
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
