# Architecture

> Deterministic Audit Runtime (DAR) — internal design reference.

## 1. System Purpose

DAR is a Python runtime that audits external code repositories for security issues. It uses LLM backends (Claude SDK, Codex) to analyze code, but the LLM is never trusted as a source of truth. Instead, every piece of LLM output enters the system as an untrusted **candidate event** that must pass a deterministic validation pipeline before it can affect audit state.

The class of failure DAR is designed to prevent: **non-reproducible, untraceable, hallucinated security findings**. If an observation appears in the final report, it must be traceable to a specific source code reference, validated by deterministic rules, and reproducible from the event log.

## 2. Architectural Thesis

Four constraints govern the design:

**Runtime is the authority.** The outer runtime (`cli.py` → `processing.py`) controls orchestration, task routing, validation, and state mutations. Backends execute bounded tasks. They do not decide what to do next, which files to inspect, or when the audit is complete.

**Proposals, not commands.** Backends produce candidate events. These are proposals that may be accepted or rejected. No backend can write to the event store or modify canonical state directly. The path is always: `backend output → normalization → validation → acceptance → event store → projection`.

**Validation before projection.** Canonical state is never written to. It is projected from the accepted event log. This means every element of state has a deterministic derivation path through the validation pipeline. If the same event log is replayed, the same state results.

**Replayability and determinism.** The event log is append-only, content-addressed, and idempotency-guarded. Given the same event log, the projector (`runtime/projector.py`) produces the same canonical state. The only non-deterministic element is the LLM backend invocation — and its output is constrained by the validation pipeline.

### System Invariants

These invariants hold at all times. If any is violated, it is a bug.

1. **No state mutation outside the event log.** Canonical state is a projection — it is never written to directly.
2. **No backend authority over state.** Backends produce proposals. Only the validation pipeline decides what enters the event log.
3. **Every accepted fact has a validation trace.** Accepted events carry an `acceptance` record naming the validator suite and reason.
4. **Projection is deterministic.** The same event log always produces the same canonical state.
5. **No silent drops.** Every observation that does not become a finding is recorded in a suppression record with an explicit reason.
6. **Provider failures do not contaminate task metrics.** Rate limits and backend errors are classified separately from correctness outcomes.

## 3. Control Plane vs Data Plane

DAR separates orchestration from data processing. This is not a distributed system, but the abstraction makes the data flow legible.

### Control Plane

Decides **what** happens next. Owns sequencing, task routing, and lifecycle.

| Component | Responsibility |
|---|---|
| `cli.py` | Command dispatch, workspace lifecycle |
| `runtime/tasks.py` | Task planner, queue management, follow-up scheduling |
| `runtime/adapters/selector.py` | Backend selection |
| `runtime/slice_builder.py` | Worker input construction (what the backend sees) |
| `runtime/coverage_tracker.py` | What has been scanned, what remains |

Control plane components are stateful — they read and write task queues, config, and workspace metadata. They are the only components that invoke external services.

### Data Plane

Decides **whether** something becomes part of canonical truth. Stateless, pure, deterministic.

| Component | Responsibility |
|---|---|
| `runtime/canonicalization.py` | Normalize to canonical form |
| `runtime/validators/*` | Validate against schemas, rules, invariants |
| `runtime/repair/*` | Best-effort repair of malformed output |
| `runtime/projector.py` | Rebuild state from events |
| `runtime/event_store.py` | Append-only log (read/write, but append-only) |
| `runtime/report_compiler.py` | Derive report from state |

Data plane components are functions: given the same input, they produce the same output. They never invoke external services and never read task queues.

### Why This Matters

The control plane can fail, retry, and reorder without corrupting state — because state is a projection of the data plane. The data plane can be tested in isolation with no mocks other than the event log.

## 4. End-to-End Pipeline

The pipeline is driven by `cli.py`, which orchestrates the following stages:

```
init-audit          Create workspace, bind policy, emit audit.created event
       │
       ▼
snapshot-target     Capture deterministic git snapshot of target repo
       │              (SHA-256 hash of all tracked file contents)
       ▼
enqueue-scan        Create module_scan tasks for target paths/files
       │              TaskPlanner writes tasks to task_queue.json
       ▼
┌─── run-task (repeated per task) ──────────────────────────┐
│                                                             │
│  1. Select next pending task from queue                     │
│  2. Build memory slice (worker input bundle)                │
│     - slice_builder.py reads file content from snapshot     │
│     - Includes worker prompt, target sources, schema        │
│  3. Invoke LLM backend via adapter                          │
│     - adapter.run_with_result(worker_role, slice_path)      │
│     - Backend produces candidate_events[]                   │
│  4. Normalize output (output_normalizer.py)                 │
│     - Transport-aware normalization per backend              │
│  5. Repair malformed candidates (repair/repairer.py)        │
│  6. Enrich candidates with snapshot metadata                │
│  7. Validate through ValidatorSuite                         │
│     - schema → duplicate → (source_binding,                 │
│       transition, contradiction)                            │
│  8. Accept or reject                                        │
│     - Accepted: appended to event log                       │
│     - Rejected: classified reason recorded in run ledger    │
│  9. Project canonical state from event log                   │
│ 10. Enqueue follow-up tasks (verification, composition)     │
│ 11. Transition task to done/failed                           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
       │
       ▼
rebuild-state       Re-project canonical state from all accepted events
       │              (idempotent — safe to run multiple times)
       ▼
compile-report      Compile final audit report from canonical state
                      - Verified observations, findings, questions
                      - Suppression records for filtered observations
                      - Consistency validation on report output
```

### Worker Roles

The pipeline uses four worker roles, each with a distinct prompt template (`prompts/`):

| Worker | Role | Output |
|---|---|---|
| Reader | Reads source code, produces observations | `observation.proposed` events |
| CandidateGenerator | Generates additional candidate proposals to expand recall | `candidate.proposed` events |
| Verifier | Verifies observations against source evidence | `observation.verified` / `observation.rejected` |
| IssueComposer | Composes verified observations into actionable issues | `issue.proposed` events |

### Task Lifecycle

```
pending → running → done
                  → failed
```

Tasks are created by `TaskPlanner` (`runtime/tasks.py`). Initial `module_scan` tasks are created by `enqueue-scan`. Follow-up tasks (verification, composition) are created automatically after successful `run-task` completion.

## 5. Core Runtime Components

### `cli.py` (1140 lines)

CLI entry point and orchestrator. Parses arguments, wires all runtime components, and executes the pipeline stages. This is the only place where the backend adapter is instantiated — via `select_and_create_adapter()`.

Key responsibilities:
- Workspace lifecycle (init, config, directories)
- Snapshot capture and binding
- Task queue management
- Backend selection (explicit `--backend` flag, no implicit fallback)
- Error handling and failure artifact capture

### `runtime/processing.py` (529 lines)

Core event processing loop. `process_candidate_events()` is the central function that:

1. Accepts a list of candidate events from the backend
2. Normalizes each event via `canonicalize_event()`
3. Runs each event through `ValidatorSuite.validate_event()`
4. Appends accepted events to the event store
5. Triggers state projection for affected audit IDs
6. Records execution traces in the run ledger

This function holds a workspace lock during execution to prevent concurrent mutation.

### `runtime/canonicalization.py` (928 lines)

Event normalization and identity derivation. Every candidate event passes through `canonicalize_event()` which:

- Normalizes display text (whitespace, unicode, casing)
- Generates deterministic entity IDs from content hashes
- Builds event fingerprints for duplicate detection
- Validates canonical event types against the allowed set
- Normalizes source references, evidence, and entity references

The canonical form is the normalized representation used for all subsequent processing. Two events that differ only in cosmetic formatting are treated as semantically equivalent.

### `runtime/event_store.py` (553 lines)

Append-only event log backed by NDJSON files. Provides:

- `append_event()` — appends with schema validation, idempotency checking, and event ID conflict detection
- `iter_events()` — reads events in append order
- Filesystem locking via `workspace_lock` to prevent concurrent writes
- Content-addressed event IDs: `event_{type}_{sha256(canonical_json)[:16]}`

The event store is the single source of truth. If it exists and is intact, the entire audit state can be reconstructed.

### `runtime/projector.py` (~350 lines)

`StateProjector` rebuilds canonical state from accepted events in append order. `build_state()` iterates all events for a given audit ID and applies each to an empty initial state via `_apply_event()`.

The projection is deterministic: same event stream → same canonical state. The result is written atomically to `state/canonical_state.json` with a SHA-256 fingerprint for integrity checking.

### `runtime/validators/` (8 files)

Deterministic validation pipeline. `ValidatorSuite` (`suite.py`) orchestrates five validators in order:

1. **SchemaValidator** (`schema.py`) — validates event structure against JSON schemas
2. **DuplicateValidator** (`duplicate.py`) — detects duplicate submissions and event ID conflicts
3. **SourceBindingValidator** (`source_binding.py`) — verifies source references have valid file paths and line ranges
4. **TransitionValidator** (`transition.py`) — enforces state machine transition rules from `rules/transition_rules.yaml`
5. **ContradictionValidator** (`contradiction.py`) — detects contradictory observations

Short-circuit semantics: schema validation fails fast. If schema fails, no other validators run. If duplicate detection fails, the event is rejected without running transition/contradiction checks.

### `runtime/repair/` (4 files)

Auto-repair layer for malformed LLM output. `Repairer` (`repairer.py`) attempts to fix common issues:

- Missing required fields (injects defaults)
- Null values where strings are expected
- Incorrect entity types (`entity_type_mapping.py`)
- Missing status fields (`status_derivation.py`)

Repair happens before validation. If repair fails, the event proceeds to validation and is rejected with a specific reason code.

### `runtime/workers/` (4 files)

Worker prompt definitions. Each file defines the input/output contract for a worker role:

- `reader.py`, `candidate_generator.py`, `verifier.py`, `issue_composer.py`

Workers are not executable — they define what the LLM backend is asked to do. The actual execution happens through the adapter layer.

### `runtime/adapters/` (7 files)

Backend abstraction layer:

| Module | Purpose |
|---|---|
| `base.py` | `BackendAdapter` protocol, `OutcomeLevel` taxonomy, policy envelope |
| `selector.py` | `BackendSelector` — explicit backend selection, no fallback |
| `capabilities.py` | Backend capability flags |
| `policy_profiles.py` | Policy-driven behavior profiles |
| `claude_agent_sdk_adapter.py` | Claude SDK adapter (stable) |
| `codex_adapter.py` | Codex CLI adapter (experimental) |

### `runtime/report_compiler.py` (762 lines)

Compiles the final audit report from canonical state. The report includes:

- Audit metadata and lifecycle status
- Verified observations with evidence chains
- Findings mapped from observations (with severity, confidence, impact)
- Suppression records for filtered observations
- Questions, contradictions, and decisions
- Candidate appendix
- Consistency validation (counts must match payload lengths)

### `runtime/pattern_scanner.py` (~250 lines)

Deterministic regex-based pre-scan that runs before the LLM Reader sees code. Scans for known vulnerability patterns (SQL injection, weak crypto, dangerous deserialization, etc.) and injects matches into worker input as concrete signal.

### `runtime/slice_builder.py` (963 lines)

Builds memory slices — the worker input bundles that contain file content, prompts, schemas, and context. Reads file content from the git snapshot, respecting file prioritization and memory budgets.

### `runtime/run_ledger.py` (925 lines)

NDJSON execution trace log. Records every worker invocation: timing, input/output digests, backend metadata, accepted/rejected event counts. Used for debugging and audit trail completeness.

### Other modules

| Module | Purpose |
|---|---|
| `outcome.py` | Outcome classification taxonomy (provider isolation) |
| `rejection.py` | Rejection reason enum and classification |
| `evidence.py` | Evidence class taxonomy and validation |
| `secret_redaction.py` | Secret/credential redaction from events and reports |
| `context_redaction.py` | Context stripping for sensitive data |
| `coverage_tracker.py` | File-level audit coverage tracking |
| `diagnostics.py` | Runtime diagnostics and debug output |
| `output_normalizer.py` | Transport-aware output normalization per backend |
| `failure_artifacts.py` | Failure bundle capture for rejected events |
| `file_prioritization.py` | File ordering heuristics for scan tasks |
| `snapshot.py` | Repository snapshot capture (SHA-256 hash tree) |
| `policies.py` | Policy engine and profile loading |
| `tasks.py` | Task queue and task planner |

## 6. State Model

### Append-Only Event Log

The event log (`events/events.ndjson`) is the single source of truth. Events are appended, never modified or deleted. Each event has:

- **Content-addressed ID**: derived from SHA-256 of the canonical JSON
- **Idempotency key**: prevents duplicate processing of the same logical event
- **Schema version**: enables forward-compatible evolution

### Canonical State as Projection

`canonical_state.json` is a derived artifact. It is computed by `StateProjector.build_state()`, which replays all accepted events for a given audit ID in append order and applies each to an empty initial state.

Canonical state is never written to directly. Any code that needs to modify state does so by appending events to the event log and triggering a re-projection.

### Idempotency and Content-Addressed Semantics

- Event IDs are derived from content, not from UUIDs. The same logical event always produces the same ID.
- The event store rejects events with conflicting IDs (same ID, different content).
- Idempotency keys ensure that re-processing the same candidate event does not produce duplicate entries.
- Projection fingerprints (SHA-256 of the canonical state JSON) enable integrity verification.

### Why Canonical State Is Never Written Directly

Direct writes would bypass the validation pipeline, break the event trail, and make the state non-reproducible. If canonical state could be written directly, there would be no way to verify that the state was derived from validated events — which would undermine the entire verification-first model.

## 7. Validation and Acceptance Model

### Candidate Events

LLM backends produce arrays of candidate events. Each candidate has type `pending` and must be processed through the validation pipeline before it can affect state.

### Validator Stages

```
candidate event
    │
    ▼
canonicalize_event()        Normalize to canonical form
    │
    ▼
_prepare_accepted_event()   Add acceptance metadata, set status to pending
    │
    ▼
ValidatorSuite.validate_event()
    ├── SchemaValidator         Fail fast → reject with SCHEMA_INVALID
    ├── DuplicateValidator      Fail fast → reject with POLICY_REJECTED
    ├── SourceBindingValidator  Non-blocking → collect issues
    ├── TransitionValidator     Non-blocking → collect issues
    └── ContradictionValidator  Non-blocking → collect issues
    │
    ▼
EventStore.append_event()   Schema validation + idempotency check
    │
    ▼
accepted → append to event log → trigger projection
rejected → record in run ledger → no state effect
```

### Rejection Classification

Every rejected event has a classified reason (`runtime/rejection.py`):

| Stage | Reason | Meaning |
|---|---|---|
| SCHEMA | `schema_invalid` | Event does not match JSON schema |
| SCHEMA | `candidate_missing` | No candidate events in output |
| TRANSPORT | `transport_rejected` | Output format invalid for transport |
| POLICY | `policy_rejected` | Violates transition rules or policy |
| POLICY | `duplicate_submission` | Same event already exists |

Each rejection is recorded with the specific validator code, enabling precise debugging.

### Repair Layer

Before validation, the repair layer (`runtime/repair/`) attempts to fix common LLM output issues: missing fields, null values, incorrect entity types. Repair is best-effort — if it fails, the event proceeds to validation and is rejected normally.

### Accepted vs Rejected Effect on State

- **Accepted**: event is appended to `events.ndjson`. The projector is triggered to rebuild `canonical_state.json`. The event becomes part of the permanent, reproducible audit trail.
- **Rejected**: event is NOT appended. The rejection reason is recorded in `runs/run_ledger.ndjson`. Canonical state is unchanged.

## 8. Adapter Boundary

### Backend Responsibility

The adapter (`runtime/adapters/`) is responsible for:

- Executing a single worker invocation with a bounded task
- Returning structured output: `candidate_events[]`, invocation metadata, digests
- Respecting the policy envelope (file access, tool use, timeout)
- Reporting outcome at four levels: process → transport → content → policy

The adapter is NOT responsible for:

- Deciding which task to run next
- Writing to the event store
- Modifying canonical state
- Selecting which events to accept or reject

### Transport/Output Normalization

Each backend has its own output format. `output_normalizer.py` provides transport-aware normalization that converts backend-specific output into the canonical candidate event format. This isolates the rest of the runtime from backend-specific quirks.

### Explicit Backend Selection

Backend selection is explicit via `--backend codex` or `--backend claude`. There is no implicit fallback. If the requested backend is unavailable, the command fails with a clear error message listing available backends.

This is a deliberate design choice: silent fallback between backends would mask cost, quality, and behavioral differences.

### Experimental Boundary

The Codex adapter (`codex_adapter.py`) is experimental. It is functional but less mature than the Claude SDK adapter. Users should expect:
- Different output quality and format
- Less robust error handling
- Possible schema mismatches requiring repair

The experimental status is marked in the adapter module and in the README.

## 9. Determinism Model

### What Is Deterministic

| Component | Deterministic? | Mechanism |
|---|---|---|
| Event canonicalization | Yes | Deterministic normalization rules, content-addressed IDs |
| Validation pipeline | Yes | Pure functions on event + state, no randomness |
| State projection | Yes | Replay events in append order → same state |
| Event ID generation | Yes | SHA-256 of canonical JSON |
| Projection fingerprint | Yes | SHA-256 of canonical state JSON |
| Snapshot capture | Yes | SHA-256 hash tree of git-tracked file contents |
| Pattern scanner | Yes | Regex matching on file content |
| Report compilation | Yes | Deterministic mapping from canonical state to report |

### What Is Not Deterministic

| Component | Why | How Constrained |
|---|---|---|
| LLM backend output | Model is non-deterministic | Output treated as untrusted proposal; must pass validation |
| Backend response time | Network, model load | Timeout budget enforced by adapter |
| Backend availability | Service status | Explicit error, no silent fallback |

### How DAR Constrains Non-Determinism

1. **Validation gate**: Every piece of LLM output must pass the deterministic validation pipeline
2. **Canonicalization**: Output is normalized to canonical form before processing
3. **Idempotency**: Same logical event can be submitted multiple times without side effects
4. **Append-only log**: No mutation or deletion of accepted events
5. **Content-addressed IDs**: Events are identified by content, not by UUID
6. **Budget enforcement**: Worker invocations have timeout limits

### Replay and Debug Implications

Because the event log is the source of truth and projection is deterministic:

- `rebuild-state` can be run at any time to re-derive canonical state from the event log
- The run ledger provides a complete execution trace with input/output digests
- Failure artifacts are captured for rejected events, enabling offline debugging
- The same event log always produces the same canonical state and the same report

## 10. Failure and Impasse Model

DAR distinguishes between **recoverable failures** (the system can continue) and **impasses** (the system cannot progress on the current path). This is not theoretical — every impasse type below maps to a specific code path and a specific outcome in the run ledger.

### Impasse Classification

| Impasse Type | What Happens | System Response | Recovery |
|---|---|---|---|
| **No viable transition** | Candidate event violates state machine rules (e.g., `observation.verified` on an entity that doesn't exist) | `TransitionValidator` rejects with `invalid_transition` or `referenced_entity_missing` | Fix the worker input or state. The task fails, but the audit continues with other tasks. |
| **Insufficient state** | Task requires a snapshot or file content that cannot be resolved | `SliceBuilder` produces empty `target_sources`; `cli.py` detects this as `SLICE_COMPLETENESS_VIOLATION` before invoking backend | Task fails with `INFRASTRUCTURE_DEFECT`. Manual investigation required. |
| **Ambiguous verification** | Verifier cannot confirm or deny an observation (contradictory evidence) | `ContradictionValidator` may reject; verifier may produce `observation.rejected` | Observation is rejected. The finding is lost unless a new candidate re-proposes it. |
| **Provider failure** | LLM backend is unavailable, times out, or returns garbage | `OutcomeClassifier` maps to `PROVIDER_FAILED` or `PROVIDER_THROTTLED` — classified separately from task failure | Provider failures do not affect task success metrics. Operator decides whether to retry. |
| **Repeated rejection loops** | Same category of candidate is repeatedly rejected (e.g., all schema validation failures) | Each rejection is recorded with reason code. Task transitions to `failed` if 0 accepted events. | Failure bundle is written to workspace. Debug offline, fix prompt or schema. |
| **Non-repairable output** | LLM output is too malformed for the repair layer to fix | `Repairer._validate_repairability()` returns `RepairRequiredError` → event is rejected at schema validation | Same as schema rejection. The repair log indicates what was wrong. |
| **Projection failure** | Events were accepted but projection crashes (e.g., state shape invariant violated) | `process_candidate_events` raises `CandidateEventProcessingError` with recovery instruction: "run rebuild-state" | Events are safe in the log. `rebuild-state` re-projects. |

### Provider Isolation

Provider failures (`PROVIDER_THROTTLED`, `PROVIDER_FAILED`) are explicitly classified as **not task failures** (`runtime/outcome.py`). They do not affect task success metrics. This prevents a flaky backend from making the audit appear to have correctness problems when the real issue is availability.

```
task success metrics:   completed_with_events, completed_no_events, runtime_failed, policy_rejected
provider metrics:       provider_throttled, provider_failed  ← separate category
```

### Failure Artifacts

When a task produces rejected events, `runtime/failure_artifacts.py` captures:
- The raw backend output
- The normalized candidate events
- The rejection reasons per event
- The run context (task ID, slice ID, worker role)

This bundle is written to the workspace for offline debugging — no need to re-run the backend to understand what went wrong.

## 11. Why Not Agent Loop

DAR deliberately avoids autonomous agent patterns. The reasoning is not ideological — it follows from the determinism requirements.

### Why No Autonomous Loops

An autonomous agent decides what to do next based on its own reasoning. This introduces an uncontrolled feedback loop: LLM output → LLM decision → LLM output. The loop has no deterministic termination condition and no external validation of the planning step.

DAR breaks this by making the **task planner** (`runtime/tasks.py`) a deterministic component that decides what to do next based on canonical state — not on LLM reasoning. The LLM only ever executes a single bounded task and returns.

```
Agent loop:    LLM → decide → LLM → decide → ...  (unbounded, non-deterministic)
DAR pipeline:  planner → LLM → validate → planner → LLM → validate  (each step bounded)
```

### Why No Planning Agents

A planning agent would use the LLM to decide which files to audit, which observations to verify, and when to stop. This puts the LLM in the control plane — exactly where DAR explicitly excludes it. File selection is done by the deterministic pattern scanner and `file_prioritization.py`. Verification routing is done by the task planner.

### Why No Memory Inside LLM

Each worker invocation is stateless. No conversation history is passed between invocations. This is required for replayability: if invocation 2 depended on invocation 1's conversation history, the event log alone would not be sufficient to reproduce the audit.

Context is provided explicitly through the memory slice (`runtime/slice_builder.py`), which is a deterministic function of the task, snapshot, and canonical state.

### What DAR Uses Instead

| Agent Pattern | DAR Equivalent |
|---|---|
| Agent decides next task | `TaskPlanner` follows deterministic rules |
| Agent remembers context | `MemorySliceBuilder` provides explicit context per invocation |
| Agent loops until done | CLI loop with task queue, each step bounded |
| Agent writes conclusions | Backend proposes → validator accepts/rejects → projector derives state |

## 12. Known Boundaries

### Current Non-Goals

- No distributed execution — single-process, single-workspace
- No real-time streaming — batch-oriented pipeline
- No web UI — CLI only
- No cross-audit correlation — each audit is isolated

### Experimental Parts

- **Codex adapter** (`runtime/adapters/codex_adapter.py`): functional but less tested
- **Pattern scanner** (`runtime/pattern_scanner.py`): evolving pattern coverage

### What DAR Does Not Claim

- No guarantee that all security issues in the target repo will be found
- No guarantee that LLM observations are factually correct — only that accepted observations pass structural validation
- No SLA on execution time — depends on backend response times
- No formal verification of the validation pipeline itself

## 13. Reading Guide (Onboarding Map)

If you are reading this codebase for the first time, here is where to start.

### Entry Points

| Want to understand... | Start here | Why |
|---|---|---|
| How a command becomes action | `cli.py` → `command_run_task()` (line ~315) | This is the main orchestrator. Every pipeline stage is visible in one function. |
| How candidate events become accepted events | `runtime/processing.py` → `process_candidate_events()` (line ~134) | The central loop. Shows normalization → validation → acceptance → projection. |
| How the event log becomes state | `runtime/projector.py` → `build_state()` (line ~67) | Replay all events → derive canonical state. Pure function. |
| How validation catches bad output | `runtime/validators/suite.py` → `validate_event()` (line ~24) | Five validators in sequence with short-circuit semantics. |
| How the backend is invoked | `runtime/adapters/base.py` → `run_with_result()` (line ~491) | The adapter protocol. Everything else is transport-specific. |
| How rejected events are classified | `runtime/rejection.py` (top docstring) | Full rejection taxonomy with pipeline stage mapping. |

### By Concept

| Concept | Path |
|---|---|
| CLI orchestrator | `cli.py` |
| Event processing loop | `runtime/processing.py` |
| Event canonicalization | `runtime/canonicalization.py` |
| Append-only event store | `runtime/event_store.py` |
| State projection | `runtime/projector.py` |
| Validation suite | `runtime/validators/suite.py` |
| Schema validation | `runtime/validators/schema.py` |
| Transition rules enforcement | `runtime/validators/transition.py` |
| Duplicate detection | `runtime/validators/duplicate.py` |
| Contradiction detection | `runtime/validators/contradiction.py` |
| Source binding validation | `runtime/validators/source_binding.py` |
| Semantic content checks | `runtime/validators/semantic_content.py` |
| Auto-repair layer | `runtime/repair/repairer.py` |
| Entity type mapping | `runtime/repair/entity_type_mapping.py` |
| Status derivation | `runtime/repair/status_derivation.py` |
| Reader worker | `runtime/workers/reader.py` |
| Candidate generator worker | `runtime/workers/candidate_generator.py` |
| Verifier worker | `runtime/workers/verifier.py` |
| Issue composer worker | `runtime/workers/issue_composer.py` |
| Adapter protocol | `runtime/adapters/base.py` |
| Backend selector | `runtime/adapters/selector.py` |
| Claude SDK adapter | `runtime/adapters/claude_agent_sdk_adapter.py` |
| Codex adapter (experimental) | `runtime/adapters/codex_adapter.py` |
| Policy profiles | `runtime/adapters/policy_profiles.py` |
| Backend capabilities | `runtime/adapters/capabilities.py` |
| Report compilation | `runtime/report_compiler.py` |
| Pattern scanner | `runtime/pattern_scanner.py` |
| Slice builder | `runtime/slice_builder.py` |
| Task planner and queue | `runtime/tasks.py` |
| Run ledger | `runtime/run_ledger.py` |
| Outcome classification | `runtime/outcome.py` |
| Rejection classification | `runtime/rejection.py` |
| Evidence taxonomy | `runtime/evidence.py` |
| Secret redaction | `runtime/secret_redaction.py` |
| Context redaction | `runtime/context_redaction.py` |
| Output normalization | `runtime/output_normalizer.py` |
| Failure artifacts | `runtime/failure_artifacts.py` |
| Coverage tracking | `runtime/coverage_tracker.py` |
| File prioritization | `runtime/file_prioritization.py` |
| Snapshot capture | `runtime/snapshot.py` |
| Policy engine | `runtime/policies.py` |
| Diagnostics | `runtime/diagnostics.py` |
| JSON schemas | `schema/*.json` |
| Policy definitions | `config/policies.yaml` |
| Transition rules | `rules/transition_rules.yaml` |
| Worker prompt templates | `prompts/*.md` |
| Test suite | `tests/` |
| Demo fixtures | `examples/demo_workspace/`, `examples/demo_single_ws/` |

### Recommended Reading Order

1. `cli.py` — understand the CLI surface and orchestration
2. `runtime/processing.py` — understand how events flow through the system
3. `runtime/validators/suite.py` — understand what gets rejected and why
4. `runtime/projector.py` — understand how state is derived from events
5. `runtime/adapters/base.py` — understand the adapter boundary
6. `runtime/report_compiler.py` — understand how reports are assembled

This order follows the data flow: command → processing → validation → projection → report.

| Concept | Path |
|---|---|
| CLI orchestrator | `cli.py` |
| Event processing loop | `runtime/processing.py` |
| Event canonicalization | `runtime/canonicalization.py` |
| Append-only event store | `runtime/event_store.py` |
| State projection | `runtime/projector.py` |
| Validation suite | `runtime/validators/suite.py` |
| Schema validation | `runtime/validators/schema.py` |
| Transition rules enforcement | `runtime/validators/transition.py` |
| Duplicate detection | `runtime/validators/duplicate.py` |
| Contradiction detection | `runtime/validators/contradiction.py` |
| Source binding validation | `runtime/validators/source_binding.py` |
| Semantic content checks | `runtime/validators/semantic_content.py` |
| Auto-repair layer | `runtime/repair/repairer.py` |
| Entity type mapping | `runtime/repair/entity_type_mapping.py` |
| Status derivation | `runtime/repair/status_derivation.py` |
| Reader worker | `runtime/workers/reader.py` |
| Candidate generator worker | `runtime/workers/candidate_generator.py` |
| Verifier worker | `runtime/workers/verifier.py` |
| Issue composer worker | `runtime/workers/issue_composer.py` |
| Adapter protocol | `runtime/adapters/base.py` |
| Backend selector | `runtime/adapters/selector.py` |
| Claude SDK adapter | `runtime/adapters/claude_agent_sdk_adapter.py` |
| Codex adapter (experimental) | `runtime/adapters/codex_adapter.py` |
| Policy profiles | `runtime/adapters/policy_profiles.py` |
| Backend capabilities | `runtime/adapters/capabilities.py` |
| Report compilation | `runtime/report_compiler.py` |
| Pattern scanner | `runtime/pattern_scanner.py` |
| Slice builder | `runtime/slice_builder.py` |
| Task planner and queue | `runtime/tasks.py` |
| Run ledger | `runtime/run_ledger.py` |
| Outcome classification | `runtime/outcome.py` |
| Rejection classification | `runtime/rejection.py` |
| Evidence taxonomy | `runtime/evidence.py` |
| Secret redaction | `runtime/secret_redaction.py` |
| Context redaction | `runtime/context_redaction.py` |
| Output normalization | `runtime/output_normalizer.py` |
| Failure artifacts | `runtime/failure_artifacts.py` |
| Coverage tracking | `runtime/coverage_tracker.py` |
| File prioritization | `runtime/file_prioritization.py` |
| Snapshot capture | `runtime/snapshot.py` |
| Policy engine | `runtime/policies.py` |
| Diagnostics | `runtime/diagnostics.py` |
| JSON schemas | `schema/*.json` |
| Policy definitions | `config/policies.yaml` |
| Transition rules | `rules/transition_rules.yaml` |
| Worker prompt templates | `prompts/*.md` |
| Test suite | `tests/` |
| Demo fixtures | `examples/demo_workspace/`, `examples/demo_single_ws/` |
