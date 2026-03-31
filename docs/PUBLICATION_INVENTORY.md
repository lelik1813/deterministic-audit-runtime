# Publication Inventory

> Generated: 2026-04-01 | Baseline commit: `9b8b4a3`

## Repository Overview

Deterministic Audit Runtime (DAR) is a Python-based system that orchestrates
reproducible repository audits via pluggable LLM backends (Claude SDK, Codex).
It enforces deterministic state machines, schema validation, and evidence
discipline across multi-worker audit pipelines.

**Scale**: ~33 800 lines of Python source + tests, 655 tracked files, ~15 MB total.

---

## Directory Map

### Entry Points

| Path | Description |
|---|---|
| `cli.py` (1100+ lines) | Main CLI. Parses args, wires runtime, orchestrates audits. Single entry point for end-users. |
| `demo.sh` / `demo.ps1` | Shell/PowerShell launchers for quick demo runs. |

### Core Runtime — `runtime/` (47 files, 1.6 MB)

The heart of the project. All business logic lives here.

```
runtime/
  adapters/             LLM backend abstraction layer
    base.py             Abstract adapter protocol
    capabilities.py     Backend capability flags
    claude_agent_sdk_adapter.py   Claude SDK integration
    codex_adapter.py    Codex backend integration
    policy_profiles.py  Policy-driven behaviour profiles
    selector.py         Backend auto-selection logic
  validators/           Multi-pass validation pipeline
    schema.py           JSON schema validation
    transition.py       State-transition rule enforcement
    duplicate.py        Deduplication logic
    contradiction.py    Cross-observation consistency
    semantic_content.py Semantic sufficiency checks
    source_binding.py   Evidence-to-source binding
    suite.py            Validator orchestration
    models.py           Shared validator data models
  repair/               Auto-repair layer for malformed outputs
    repairer.py         Main repair orchestrator
    entity_type_mapping.py  Entity type normalisation
    status_derivation.py    Status inference heuristics
    types.py            Repair-specific types
  workers/              Audit pipeline stages
    reader.py           Source code reader worker
    candidate_generator.py  Candidate expansion worker
    verifier.py         Verification worker
    issue_composer.py   Issue composition worker
  canonicalization.py   Output normalisation
  compose_issue_contract.py  Issue contract enforcement
  context_redaction.py  Sensitive context stripping
  coverage_tracker.py   Audit coverage tracking
  diagnostics.py        Runtime diagnostics
  event_store.py        Append-only event log
  evidence.py           Evidence chain management
  failure_artifacts.py  Failure artifact capture
  file_prioritization.py  File ordering heuristics
  outcome.py            Audit outcome model
  output_normalizer.py  Transport-aware output normalisation
  pattern_scanner.py    Static pattern scanning
  policies.py           Policy engine
  processing.py         Main processing loop
  projector.py          State projection from events
  rejection.py          Rejection handling
  report_compiler.py    Final report assembly
  run_ledger.py         Run metadata ledger
  secret_redaction.py   Secret/credential redaction
  slice_builder.py      Workspace slicing
  snapshot.py           State snapshot utilities
  tasks.py              Task queue management
```

**Publication value**: High. This is the core IP and the primary artifact for public release.

### Schemas — `schema/` (7 files, 113 KB)

JSON schemas that define the wire format and validation contracts.

| Schema | Purpose |
|---|---|
| `audit.schema.json` | Top-level audit result schema |
| `candidate.schema.json` | Candidate proposal schema |
| `event.schema.json` | Event store entry schema |
| `report.schema.json` | Final report schema |
| `worker_input.schema.json` | Worker input contract |
| `worker_output.schema.json` | Worker output contract |
| `codex_transport_output.schema.json` | Codex transport envelope |

**Publication value**: High. Schemas are the API contract; essential for anyone consuming or extending DAR.

### Tests — `tests/` (36 files, 1.2 MB)

Comprehensive pytest-based test suite covering adapters, validators, repair,
report compilation, redaction, policies, and end-to-end flows.

- `tests/fixtures/golden_report_security_mini.json` — golden file for e2e regression
- Tests are well-named and map 1:1 to runtime modules

**Publication value**: High. Demonstrates correctness and serves as usage examples.

### Configuration — `config/`, `rules/`, `prompts/`

| Path | Files | Description |
|---|---|---|
| `config/policies.yaml` | 1 | Policy profiles (thresholds, behaviour flags) |
| `rules/transition_rules.yaml` | 1 | State machine transition rules |
| `prompts/` | 4 | Worker prompt templates (reader, candidate_generator, verifier, issue_composer) |

**Publication value**: Medium. Essential for runtime operation; prompts show how LLM integration is structured.

### Documentation — `docs/` (8 files, 72 KB)

| Document | Content |
|---|---|
| `ADR/001-claude-sdk-integration-boundary.md` | Architecture decision record |
| `AUDIT_STATUS_TRANSITION_POLICY.md` | Status transition policy spec |
| `OBSERVATION_TO_FINDING_MAPPING_CONTRACT.md` | Observation-to-finding mapping |
| `REPORT_RUNTIME_CONTRACT.md` | Report compilation contract |
| `REPORT_SCHEMA_MIGRATION_NOTES_STEP2.md` | Schema migration notes |
| `TODO_VERIFIED_OBSERVATIONS_TO_FINDINGS.md` | Outstanding work item |
| `non_goals.md` | Explicit non-goals |
| `v1_2_invariants.md` | Version invariants |

**Publication value**: Medium-High. ADR and contracts are valuable; some docs are internal WIP notes.

### Scripts — `_scripts/` (2 files, 12 KB)

- `run_claude_manual_auto_target.ps1` — manual Claude backend test launcher
- `run_codex_manual_auto_target.ps1` — manual Codex backend test launcher

Both contain **hardcoded local Windows paths** (`C:\Users\rocki\...`).

**Publication value**: Low. Machine-specific; need path generalisation before publishing.

---

## Workspace Directories

These follow a uniform layout used by the runtime for audit execution:

```
<workspace>/
  audit_config.json     Per-audit configuration
  config/policies.yaml  (inherited or overridden policies)
  events/events.ndjson  Append-only event log
  prompts/              Per-worker prompt overrides
  reports/              Generated audit reports
  rules/                Per-audit transition rules
  runs/run_ledger.ndjson  Run metadata
  schema/               Workspace-local schema copies
  state/
    canonical_state.json       Current state
    projections/               Historical state snapshots (many)
    slices/                    Per-task state slices (many)
    task_queue.json            Task queue
```

| Workspace | Files | Size | Nature |
|---|---|---|---|
| `demo_workspace/` | 21 | 267 KB | Demo fixture — clean example |
| `demo_single_ws/` | 20 | 206 KB | Single-worker demo fixture |
| `claude_manual_ws/` | 24 | 442 KB | Manual Claude test run output |
| `codex_manual_ws/` | 120 | 3.3 MB | Manual Codex test run output (large) |
| `test_mini/` | 124 | 2.4 MB | Minimal test run output |
| `test_mini_20260331_125725/` | 63 | 1.1 MB | Timestamped test run output |
| `test_mini_20260331_131011/` | 20 | 270 KB | Timestamped test run output |
| `test_mini_20260331_155716/` | 26 | 386 KB | Timestamped test run output |
| `test_mini_20260331_180738/` | 122 | 2.4 MB | Timestamped test run output |

**Publication value**: `demo_workspace/` and `demo_single_ws/` serve as reference fixtures.
All others are generated run artifacts with no public value.

---

## Candidates for Removal / Exclusion Before Publication

### Generated Artifacts & Run Output

| Path | Reason |
|---|---|
| `claude_manual_ws/` | Manual test output — not a clean example |
| `codex_manual_ws/` | Manual test output, 3.3 MB of projections/slices |
| `test_mini/` | Generated run output |
| `test_mini_20260331_125725/` | Timestamped run debris |
| `test_mini_20260331_131011/` | Timestamped run debris |
| `test_mini_20260331_155716/` | Timestamped run debris |
| `test_mini_20260331_180738/` | Timestamped run debris |

### Machine-Specific / Local-Only

| Path | Reason |
|---|---|
| `_scripts/run_claude_manual_auto_target.ps1` | Hardcoded `C:\Users\rocki\...` path |
| `_scripts/run_codex_manual_auto_target.ps1` | Hardcoded `C:\Users\rocki\...` path |
| `_user_reply/` | Agent instruction files — internal workflow |
| `.claude/` | Claude Code session settings — not for publication |

### Cached / Build Artifacts (already gitignored)

| Path | Status |
|---|---|
| `__pycache__/` | In `.gitignore` but committed in baseline |
| `runtime/**/__pycache__/` | Same |
| `.pytest_cache/` | Same |

These should be added to `.gitignore` properly (or verified) and removed from tracking.

### Potential Config Risks

| Path | Risk | Mitigation |
|---|---|---|
| `config/policies.yaml` | Low — no secrets, just thresholds | Review before publish |
| `rules/transition_rules.yaml` | None — declarative state machine rules | Safe to publish |
| Workspace `audit_config.json` files | May contain absolute local paths | Scrub or exclude |

### Misc

| Path | Reason |
|---|---|
| `Deterministic Audit Runtime (DAR) repository structure and publication-scope audit.txt` (447 KB) | One-time analysis artifact, not part of the project |
| `docs/TODO_VERIFIED_OBSERVATIONS_TO_FINDINGS.md` | Internal WIP, incomplete |
| `docs/REPORT_SCHEMA_MIGRATION_NOTES_STEP2.md` | Internal migration notes |

---

## Keep As-Is

These directories/files are publication-ready with no changes needed:

- `runtime/` — core source code
- `schema/` — JSON schemas
- `tests/` — test suite
- `cli.py` — entry point
- `demo.sh` / `demo.ps1` — demo launchers (path-agnostic)
- `README.md` — project readme
- `.gitignore` — standard ignores
- `docs/adr/` — architecture decision records
- `docs/AUDIT_STATUS_TRANSITION_POLICY.md`
- `docs/OBSERVATION_TO_FINDING_MAPPING_CONTRACT.md`
- `docs/REPORT_RUNTIME_CONTRACT.md`
- `docs/v1_2_invariants.md`
- `docs/non_goals.md`
- `prompts/` — worker prompt templates
- `config/policies.yaml`
- `rules/transition_rules.yaml`
- `demo_workspace/` — clean demo fixture
- `demo_single_ws/` — clean single-worker fixture

---

## Documentation Surface Assessment

**Rating: Small**

Rationale:
- Only 8 documentation files in `docs/`, totalling ~72 KB
- No API reference, no contributor guide, no changelog
- One ADR, four contract/spec documents, one non-goals, one invariants doc, one TODO
- `README.md` exists but is likely the primary documentation surface
- The codebase is heavily self-documenting through test names and schema definitions

The documentation surface is **small** but **dense** — most docs are formal
contracts and specifications rather than tutorials or guides. The primary gap
for publication is the absence of:
1. Architecture overview / getting-started guide
2. CONTRIBUTING.md
3. CHANGELOG.md
4. API reference (runtime modules are undocumented externally)

---

## Summary Statistics

| Category | Count | Size |
|---|---|---|
| Runtime source modules | ~30 `.py` files | 1.6 MB |
| Adapters | 7 `.py` files | — |
| Validators | 8 `.py` files | — |
| Workers | 4 `.py` files | — |
| Repair layer | 4 `.py` files | — |
| Tests | 36 `.py` files | 1.2 MB |
| Schemas | 7 `.json` files | 113 KB |
| Documentation | 8 `.md` files | 72 KB |
| Workspaces (all) | 9 directories | ~9.5 MB |
| Workspaces (keep) | 2 directories | ~470 KB |
| Candidates for removal | 7 directories + 5 files | ~10 MB |
