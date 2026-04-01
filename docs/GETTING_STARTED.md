# Getting Started

This guide walks you through installing DAR, running the demo, auditing your own project, and understanding the output.

For architecture and design rationale, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Required |
| Git | any | Required for target repo snapshots |
| `jsonschema` | 4.x | `pip install jsonschema` |
| `PyYAML` | 6.x | `pip install pyyaml` |
| `referencing` | 0.x | `pip install referencing` (jsonschema dependency) |
| LLM backend | — | One of the following (see below) |

### Backend Options

You need at least one LLM backend installed and configured:

**Claude SDK** (recommended):
```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

**Codex CLI** (experimental):
```bash
npm install -g @openai/codex
export OPENAI_API_KEY=sk-...
```

### Install Dependencies

```bash
pip install jsonschema pyyaml referencing anthropic
```

No `setup.py` or `pip install -e .` is needed — DAR runs directly from the repository root using `python cli.py`.

## Running the Demo

The fastest way to see DAR in action is the included demo script. It runs the full pipeline against a target repository.

### Prerequisites for the demo

You need a git repository to audit. The demo script auto-detects a sibling directory called `runtime-audit-fixture`, or you can pass any repo path.

### Linux / macOS

```bash
# Auto-detect sibling target repo
./examples/demo.sh

# Or point at any git repo
./examples/demo.sh /path/to/your/project
```

### Windows PowerShell

```powershell
# Auto-detect sibling target repo
.\examples\demo.ps1

# Or point at any git repo
.\examples\demo.ps1 -TargetRepo C:\path\to\your\project
```

### What the demo does

The script runs six steps:

```
1. init-audit       →  creates workspace, binds policy
2. snapshot-target  →  captures deterministic git snapshot (SHA-256 of all files)
3. enqueue-scan     →  auto-detects target files, creates scan tasks
4. run-task         →  invokes LLM backend, validates candidates, updates state
5. rebuild-state    →  re-derives canonical state from event log
6. compile-report   →  assembles final audit report
```

If it succeeds, you'll see:
```
  ✓ Demo complete.
```

If it fails, check [Common Failure Modes](#common-failure-modes) below.

## Running a Manual Audit (CLI)

For real-world use, you run the pipeline steps individually. This gives you control over which files to scan and which backend to use.

### Step 1: Initialize the workspace

```bash
python cli.py init-audit \
  --workspace my_audit \
  --target-repo /path/to/your/project \
  --audit-id my-project-001 \
  --title "Security audit of my-project" \
  --policy low_noise
```

This creates the `my_audit/` directory with config, schemas, and an empty event log. The `audit.created` event is written and the initial state is projected.

Policy options: `strict_security`, `low_noise`, `exploratory`.

### Step 2: Capture a snapshot

```bash
python cli.py snapshot-target --workspace my_audit
```

This captures a deterministic snapshot of the target repo at the current git HEAD. The snapshot is a SHA-256 hash tree of all tracked files — it ensures the audit is bound to a specific commit.

### Step 3: Create scan tasks

```bash
# Scan a specific path
python cli.py enqueue-scan \
  --workspace my_audit \
  --target-kind path \
  --targets src/

# Scan multiple paths
python cli.py enqueue-scan \
  --workspace my_audit \
  --target-kind path \
  --targets src/ app/ config/
```

This creates `module_scan` tasks in the task queue. Each task covers one target path.

### Step 4: Run tasks

```bash
# Run a single task
python cli.py run-task --workspace my_audit --backend claude

# Run all tasks in a loop
python cli.py run-all-tasks --workspace my_audit --backend claude --max-iterations 50
```

Each `run-task` invocation:
1. Claims the next pending task from the queue
2. Builds a worker input bundle (memory slice) with file content from the snapshot
3. Invokes the LLM backend with the Reader worker prompt
4. Validates all candidate events through the deterministic pipeline
5. Appends accepted events to the event log
6. Projects updated canonical state
7. Enqueues follow-up tasks (verification, issue composition) if candidates were accepted

The task queue drives follow-up work automatically: Reader → CandidateGenerator → Verifier → IssueComposer.

### Step 5: Rebuild state (optional)

```bash
python cli.py rebuild-state --workspace my_audit
```

Re-derives canonical state from the entire event log. This is idempotent and safe to run multiple times. Use it to recover state if something went wrong during processing.

### Step 6: Compile the report

```bash
python cli.py compile-report --workspace my_audit
```

Generates the final audit report in `my_audit/reports/`.

## Understanding the Workspace Output

After running the pipeline, the workspace contains:

```
my_audit/
├── audit_config.json              # Audit config (ID, policy, target repo path)
├── config/
│   └── policies.yaml              # Policy thresholds (copied from config/)
├── events/
│   └── events.ndjson              # Append-only event log (source of truth)
├── state/
│   ├── canonical_state.json       # Projected state from accepted events
│   ├── task_queue.json            # Task lifecycle (pending → running → done/failed)
│   ├── projections/               # Historical state snapshots (one per projection)
│   └── slices/                    # Worker input bundles (replayable)
├── runs/
│   └── run_ledger.ndjson          # Execution traces with digests and timing
├── reports/
│   └── report.<audit_id>.json     # Final audit report
└── schema/                        # JSON schemas (copied from schema/)
```

### The event log (`events/events.ndjson`)

This is the single source of truth. Every accepted event is appended here in order. Events are never modified or deleted. If this file exists and is intact, the entire audit state can be reconstructed.

Each line is a JSON object with fields like:
- `id` — content-addressed event ID
- `event_type` — e.g., `audit.created`, `observation.proposed`, `observation.verified`
- `entity_id` — the entity this event affects
- `payload` — event-specific data
- `acceptance` — who accepted it and why

### Canonical state (`state/canonical_state.json`)

This is a derived artifact — a projection of the event log. It contains the current state of all entities (audit, observations, questions, issues, etc.). It is never written to directly; it is always re-derived from events.

Top-level keys:
```json
{
  "schema_version": "1.0.0",
  "audit": { "id": "...", "status": "...", "target": {...} },
  "observations": { "obs_<hash>": {...}, ... },
  "questions": { "question_<hash>": {...}, ... },
  "issues": {},
  "hypotheses": {},
  "decisions": {},
  "candidates": {},
  "tasks": {},
  "contradictions": {}
}
```

### The report (`reports/report.<audit_id>.json`)

The final audit report assembled from canonical state. Top-level structure:

```json
{
  "report_id": "report_<hash>",
  "source_audit_id": "my-project-001",
  "schema_version": "1.0.0",
  "audit_status": "analyzed",
  "summary": {
    "verified_observation_count": 5,
    "finding_count": 3,
    "open_question_count": 1,
    ...
  },
  "verified_observations": [...],
  "findings": [...],
  "open_questions": [...],
  "contradictions": [...],
  "decisions": [...],
  "suppression_records": [...]
}
```

## How to Read the Report

### What the output looks like

Here are three concrete examples of what you'll find in the report.

**A verified observation** (in `verified_observations`):
```json
{
  "observation_id": "obs_a1b2c3d4",
  "statement": "SQL query constructed via string concatenation with user input",
  "status": "verified",
  "confidence": "high",
  "origin": "model_discovered",
  "source_refs": [
    { "file_path": "app/db.py", "line_range": { "start": 42, "end": 45 } }
  ],
  "evidence": [
    { "class": "direct_code_fact", "description": "f-string interpolation of user_id into SQL query" }
  ]
}
```

**A finding** (in `findings`):
```json
{
  "finding_id": "finding_e5f6a7b8",
  "title": "SQL injection via string concatenation in app/db.py",
  "severity": "high",
  "confidence": "high",
  "status": "open",
  "source_observation_ids": ["obs_a1b2c3d4"],
  "recommended_fix": "Use parameterized queries instead of string concatenation"
}
```

**A suppression record** (in `suppression_records`):
```json
{
  "observation_id": "obs_c9d0e1f2",
  "reason": "below_threshold",
  "details": "Observation severity is 'info', below 'low' threshold for policy 'low_noise'"
}
```

Key takeaway: observations are **statements about code** with evidence. Findings are **actionable issues** derived from observations. Suppression records explain why an observation did not become a finding — nothing is silently dropped.

### Observations vs Findings

These are different concepts:

| Concept | What it is | Status |
|---|---|---|
| **Observation** | A statement about the code, backed by source evidence | `proposed` → `verified` or `rejected` |
| **Finding** | An actionable security issue derived from verified observations | `open`, `accepted_risk`, `fixed`, `false_positive` |

Not every observation becomes a finding. Observations that are too low-risk, duplicates, or suppressed by policy are recorded in `suppression_records` — they are never silently dropped.

### Suppression Records

If a verified observation does not become a finding, a suppression record explains why:

```json
{
  "observation_id": "obs_abc123",
  "reason": "below_threshold",
  "details": "Observation severity is 'info', below 'low' threshold for policy 'low_noise'"
}
```

Reasons: `below_threshold`, `duplicate`, `policy_suppressed`, `insufficient_evidence`.

### Report Consistency

The report compiler validates consistency before writing. If `summary.finding_count` does not match the actual `findings` array length, report compilation fails with a clear error. This prevents misleading reports.

## Common Failure Modes

### Backend not found

```
Backend 'claude' unavailable: anthropic package not installed.
```

**Fix:** Install the backend package:
```bash
pip install anthropic   # for Claude SDK
npm install -g @openai/codex  # for Codex
```

If neither backend is available, use `--backend codex` or `--backend claude` to specify which one to use.

### No API key set

```
Backend 'claude' unavailable: ANTHROPIC_API_KEY not set.
```

**Fix:** Set the environment variable:
```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Claude
export OPENAI_API_KEY=sk-...          # Codex
```

### Empty results (no observations accepted)

```
run-task produced no accepted candidate events for task 'task_abc123'.
```

This means the LLM backend returned output, but all candidates were rejected by the validation pipeline. Check `runs/run_ledger.ndjson` for rejection reasons, or look at failure artifacts in the workspace.

Common causes:
- Backend returned malformed JSON → rejected at schema validation
- Backend returned observations without source references → rejected at source binding
- Backend returned observations for the wrong audit → rejected at audit alignment check

### Target repo not a git repository

```
Not a git repo: /path/to/something
```

**Fix:** DAR requires the target to be a git repository with a clean working tree. Commit or stash your changes, then re-run.

### Snapshot mismatch

```
SLICE_COMPLETENESS_VIOLATION: module_scan task has no target_sources.
```

This means the target repo has changed since the snapshot was taken (files were removed or modified). Re-run `snapshot-target` to capture a fresh snapshot.

### Validation rejects all output

If the backend consistently produces output that fails validation, check:

1. **Schema version mismatch** — ensure `schema/` in the workspace matches the runtime version
2. **Backend format change** — if the LLM provider changed output format, the adapter or normalizer may need updating
3. **Policy too strict** — try `--policy exploratory` for looser thresholds

### Projection failure

```
Accepted events were persisted, but canonical state projection failed.
Run rebuild-state to recover canonical_state.json from the accepted event log.
```

Events are safe in the log. Run `rebuild-state` to recover. This is a defensive measure — it means events were accepted but the projection step hit an unexpected state shape.

## Next Steps

- Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design reference
- Read [`docs/non_goals.md`](non_goals.md) for explicit boundaries
- Browse `examples/demo_workspace/` for a concrete example of workspace output
- Run `python -m pytest tests/ -v` to see the full test suite
