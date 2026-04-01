# Deterministic Audit Runtime (DAR)

> **DAR in one sentence:** A deterministic runtime that treats LLM output as proposals and accepts only what passes validation into an event-sourced state.

A verification-first audit runtime that uses LLM backends as **bounded workers** — not as authorities. All outputs are proposals. Only validated and accepted events define canonical state. The entire audit trail is event-sourced and replayable.

## Problem

LLM-based code analysis tools treat model output as ground truth. This creates brittle, non-reproducible results: the same codebase audited twice produces different findings, with no way to trace why a particular conclusion was reached or to verify its correctness after the fact.

### Real-world failure: fabricated evidence in AI-generated reports

In 2025, a Deloitte report produced for the Australian government was found to contain fabricated citations, nonexistent academic sources, and incorrect references.

- https://www.theguardian.com/australia-news/2025/oct/06/deloitte-to-pay-money-back-to-albanese-government-after-using-ai-in-440000-report
- https://www.ndtv.com/world-news/deloittes-ai-fallout-explained-the-440-000-report-that-backfired-9417098

The failure was not limited to model quality. It was architectural:

- LLM-generated content was treated as authoritative
- there was no verification layer
- no binding between claims and evidence
- no audit or replay mechanism before delivery

This project targets this class of failure directly.

The runtime enforces a different execution model:

- LLM outputs are treated as proposals, not facts
- all state transitions require validation
- accepted knowledge must be backed by verifiable evidence
- system state is externalized, replayable, and auditable

This prevents unverified or hallucinated content from propagating into final outputs.

### Architectural lineage: ACT-R and Soar

This runtime follows principles established in classical cognitive architectures such as ACT-R and Soar.

In these systems:

- memory is external and structured (not implicit in the model)
- reasoning operates over explicit state
- transitions are governed by rules and constraints
- cognition proceeds through a controlled cycle rather than free-form generation

This project applies the same separation to LLM-based systems:

- state and memory are externalized and owned by the runtime
- the LLM does not act as a controller or source of truth
- reasoning is expressed as bounded proposals
- progression is governed by deterministic transitions and validation

In effect:

```
LLMs provide candidate hypotheses
The runtime validates, records, and decides
```

This preserves inspectability, reproducibility, and control — properties required for high-risk or correctness-sensitive systems.

## Core Thesis

DAR inverts the trust model:

- The **runtime** is the authority — it controls orchestration, validation, and state
- The **LLM backend** is a bounded execution engine with no authority over audit state
- All LLM output enters the system as **candidate events** that must pass a deterministic validation pipeline before acceptance
- Canonical state is a **projection** of accepted events — never written directly
- The same event log always produces the same state

## Key Capabilities

| Capability | Status |
|---|---|
| Event-sourced audit trail (append-only, content-addressed) | Implemented |
| Multi-pass validation pipeline (schema, transition, duplicate, contradiction, source binding) | Implemented |
| Deterministic state projection from event log | Implemented |
| Pluggable LLM backends (Claude SDK, Codex) with explicit selection — no silent fallback | Implemented |
| Worker pipeline: Reader, CandidateGenerator, Verifier, IssueComposer | Implemented |
| Deterministic pattern scanner (regex pre-scan before LLM) | Implemented |
| Auto-repair layer for malformed worker output | Implemented |
| Policy profiles (strict_security, low_noise, exploratory) | Implemented |
| Secret and context redaction | Implemented |
| Audit report compilation with suppression tracking | Implemented |
| CLI orchestration with workspace isolation | Implemented |
| Codex adapter | **Experimental** |
| ADR-based architecture decisions | Implemented |

## Non-Goals

DAR explicitly does **not**:

- Give LLM backends control over orchestration or task routing
- Allow direct writes to canonical state — all mutations go through validation
- Maintain conversational memory between worker invocations
- Bypass validation for any backend
- Silently fall back between backends on failure
- Support autonomous agent loops — every invocation is budget-bounded

See [`docs/non_goals.md`](docs/non_goals.md) for the full list with rationale.

## Repository Structure

```
cli.py                  # CLI entry point and orchestrator
runtime/                # Core runtime
  adapters/             # Backend abstraction (Claude SDK, Codex)
    base.py             # Adapter protocol and outcome taxonomy
    selector.py         # Explicit backend selection (no implicit fallback)
    claude_agent_sdk_adapter.py
    codex_adapter.py    # Experimental
  validators/           # Deterministic validation pipeline
  workers/              # Audit pipeline stages
  repair/               # Auto-repair for malformed output
  processing.py         # Main event processing loop
  projector.py          # Deterministic state projection
  canonicalization.py   # Event normalization
  report_compiler.py    # Report assembly
  event_store.py        # Append-only event log
  pattern_scanner.py    # Pre-LLM deterministic pattern detection
  ...                   # (coverage, diagnostics, redaction, etc.)
schema/                 # JSON schemas (event, report, candidate, worker I/O)
config/policies.yaml    # Policy profiles
rules/transition_rules.yaml  # State machine transition rules
prompts/                # Worker prompt templates
examples/
  demo_workspace/       # Full demo workspace (sanitized)
  demo_single_ws/       # Single-file demo workspace (sanitized)
  demo.sh / demo.ps1    # Demo launchers
tests/                  # pytest suite (36 test files)
docs/                   # Architecture docs, contracts, ADRs
```

## Quick Start

### Prerequisites

- Python 3.10+
- Git
- An LLM backend: Claude SDK (`claude` package) **or** Codex CLI (`codex` + `OPENAI_API_KEY`)

### Run the Demo

```bash
# Linux/macOS — auto-detects a sibling target repository
./examples/demo.sh

# Windows PowerShell
.\examples\demo.ps1

# Or specify a target explicitly
./examples/demo.sh --target-repo /path/to/your/project
```

The demo runs the full pipeline:

1. **init-audit** — creates workspace, binds policy
2. **snapshot-target** — captures deterministic git snapshot
3. **enqueue-scan** — detects targets, creates scan tasks
4. **run-task** — executes LLM backend, produces candidate events
5. **rebuild-state** — projects canonical state from accepted events
6. **compile-report** — generates final audit report

### Minimal CLI Flow

```bash
# Initialize workspace
python cli.py init-audit \
  --workspace my_audit \
  --target-repo /path/to/project \
  --audit-id my-audit-001 \
  --policy low_noise

# Capture snapshot
python cli.py snapshot-target --workspace my_audit

# Create scan tasks
python cli.py enqueue-scan --workspace my_audit \
  --target-kind path --targets src/

# Run tasks (explicit backend selection)
python cli.py run-task --workspace my_audit --backend claude
python cli.py run-task --workspace my_audit --backend claude

# Rebuild state and compile report
python cli.py rebuild-state --workspace my_audit
python cli.py compile-report --workspace my_audit
```

Backend selection is explicit. There is no implicit fallback — if the requested backend is unavailable, the command fails with a clear error.

### Workspace Output

```
my_audit/
  audit_config.json          # Audit metadata + policy binding
  events/events.ndjson        # Append-only event log (source of truth)
  state/
    canonical_state.json      # Deterministic projection of accepted events
    task_queue.json           # Task lifecycle
    projections/              # Historical state snapshots
    slices/                   # Worker input bundles (replayable)
  runs/run_ledger.ndjson      # Execution traces with digests
  reports/report.my-audit-001.json
```

## Testing

```bash
# Full test suite
python -m pytest tests/ -v

# Smoke test with mocked backend (no API key needed)
python -m pytest tests/test_codex_demo_smoke.py -v

# End-to-end security fixture
python -m pytest tests/test_report_security_mini_e2e -v
```

The test suite covers adapters, validators, repair, report compilation, redaction, policies, and end-to-end flows. All tests use mocked backends — no API keys required.

## Documentation

| Document | Description |
|---|---|
| [`docs/non_goals.md`](docs/non_goals.md) | What DAR explicitly does not do |
| [`docs/adr/001-claude-sdk-integration-boundary.md`](docs/adr/001-claude-sdk-integration-boundary.md) | ADR: Claude SDK integration boundary |
| [`docs/AUDIT_STATUS_TRANSITION_POLICY.md`](docs/AUDIT_STATUS_TRANSITION_POLICY.md) | Audit lifecycle status transitions |
| [`docs/REPORT_RUNTIME_CONTRACT.md`](docs/REPORT_RUNTIME_CONTRACT.md) | Report compilation contract |
| [`docs/OBSERVATION_TO_FINDING_MAPPING_CONTRACT.md`](docs/OBSERVATION_TO_FINDING_MAPPING_CONTRACT.md) | Observation-to-finding mapping rules |
| [`docs/v1_2_invariants.md`](docs/v1_2_invariants.md) | Version invariants |
| [`ARCHITECTURAL_POSITIONING.md`](ARCHITECTURAL_POSITIONING.md) | Concise architectural argument and positioning |

## Maturity

**DAR is in active development.** The core runtime, validation pipeline, and Claude SDK adapter are stable and tested. The following components are experimental:

- **Codex adapter** (`runtime/adapters/codex_adapter.py`) — functional but less mature than the Claude SDK adapter
- **Pattern scanner** (`runtime/pattern_scanner.py`) — deterministic regex layer; pattern coverage is evolving

No API stability guarantees yet. Schema versions are tracked in `schema/` and in `docs/REPORT_SCHEMA_MIGRATION_NOTES_STEP2.md`.
