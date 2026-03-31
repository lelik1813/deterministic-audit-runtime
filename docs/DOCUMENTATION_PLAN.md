# Documentation Plan

> Generated: 2026-04-01 | Based on PUBLICATION_INVENTORY.md, baseline `9b8b4a3`

## Priority Definitions

| Priority | Meaning |
|---|---|
| **P0** | Must exist before public repository publish. Without these, the repo is unusable. |
| **P1** | Should exist at or shortly after publish. Important for adoption and correctness. |
| **P2** | Nice to have. Improves professionalism and long-term maintainability. |

---

## Documentation Units

### P0 — Public-Facing Docs

#### DOC-01: README.md (rewrite)

| Field | Value |
|---|---|
| **Purpose** | Project overview, quickstart, and entry point for all users |
| **Audience** | First-time visitors, potential users, evaluators |
| **Modules covered** | Top-level: `cli.py`, workspace layout, config |
| **Required sections** | What it does / Quick start / CLI usage / Workspace anatomy / Architecture overview (diagram) / Configuration / Running tests / Contributing |
| **Can skip** | Internal module details, adapter internals, validator pipeline |

Current README exists but likely needs expansion for public consumption.

---

#### DOC-02: Architecture Overview

| Field | Value |
|---|---|
| **Purpose** | Explain the end-to-end audit pipeline and data flow |
| **Audience** | Developers integrating DAR, security researchers, architects |
| **Modules covered** | `cli.py` → `processing.py` → `workers/*` → `validators/*` → `projector.py` → `report_compiler.py` |
| **Required sections** | Pipeline diagram / Event-sourcing model / Worker stages / Validation pipeline / State projection / Report compilation / Adapter boundary |
| **Can skip** | Individual validator logic details, repair internals |
| **File** | `docs/ARCHITECTURE.md` |

Key concepts to explain:
- Deterministic audit = same input always produces same output
- Event-sourced state (append-only events → projected canonical state)
- Worker pipeline: Reader → CandidateGenerator → Verifier → IssueComposer
- Hybrid approach: deterministic pattern scan + LLM reasoning
- Adapter isolation: backends are bounded execution engines with NO state authority

---

#### DOC-03: Getting Started Guide

| Field | Value |
|---|---|
| **Purpose** | Step-by-step guide to run first audit |
| **Audience** | New users |
| **Modules covered** | `cli.py`, workspace setup, config, schemas |
| **Required sections** | Prerequisites / Installation / Running a demo / Understanding the output / Next steps |
| **Can skip** | Internals, extension points |
| **File** | `docs/GETTING_STARTED.md` |

---

### P1 — Architecture & Developer Docs

#### DOC-04: Adapter Boundary Specification

| Field | Value |
|---|---|
| **Purpose** | Define the contract between runtime and LLM backends |
| **Audience** | Contributors adding new backends, integrators |
| **Modules covered** | `runtime/adapters/base.py`, `selector.py`, `capabilities.py`, `policy_profiles.py`, `claude_agent_sdk_adapter.py`, `codex_adapter.py` |
| **Required sections** | BackendAdapter protocol / Outcome levels (process → transport → content → policy) / Capability flags / Selection logic / Adding a new backend / Transport contract per backend |
| **Can skip** | Adapter-internal implementation details |
| **File** | `docs/ADAPTER_BOUNDARY.md` |

This replaces and extends `docs/adr/001-claude-sdk-integration-boundary.md`.

---

#### DOC-05: Event Sourcing & State Projection

| Field | Value |
|---|---|
| **Purpose** | Explain the event-sourcing model, projection semantics, and state invariants |
| **Audience** | Core developers, anyone debugging state issues |
| **Modules covered** | `event_store.py`, `projector.py`, `canonicalization.py`, `snapshot.py` |
| **Required sections** | Event lifecycle / Canonicalization rules / Projection algorithm / Snapshot semantics / Idempotency & conflict detection / State schema |
| **Can skip** | JSON schema details (reference schemas directly) |
| **File** | `docs/EVENT_SOURCING.md` |

Key invariants to document:
- Events are append-only; no mutation or deletion
- Projection is deterministic: same event stream → same canonical state
- Event ID = SHA-256 of canonical JSON (content-addressed)
- Idempotency keys prevent duplicate processing
- Filesystem locking for concurrent workspace access

---

#### DOC-06: Validation Pipeline

| Field | Value |
|---|---|
| **Purpose** | Document the multi-pass validation that every candidate event passes through |
| **Audience** | Developers modifying validation logic, understanding rejection reasons |
| **Modules covered** | `runtime/validators/*` (suite, schema, transition, duplicate, contradiction, semantic_content, source_binding) |
| **Required sections** | Validation order / Short-circuit semantics / Per-validator rules / ValidationIssue model / Transition rules YAML reference / Extending validators |
| **Can skip** | Individual test case documentation |
| **File** | `docs/VALIDATION_PIPELINE.md` |

Short-circuit order: schema → duplicate → (source_binding, transition, contradiction) in parallel.

---

#### DOC-07: Acceptance & Rejection Flow

| Field | Value |
|---|---|
| **Purpose** | Explain the full lifecycle of a candidate from proposal to acceptance/rejection |
| **Audience** | All developers working on the runtime |
| **Modules covered** | `rejection.py`, `outcome.py`, `processing.py`, `canonicalization.py`, `repair/repairer.py` |
| **Required sections** | Pipeline stages (parse → schema → candidate → policy → transport) / Rejection reason enum / Outcome taxonomy / Repair layer / Failure artifacts / Provider isolation |
| **Can skip** | Validator internals (covered by DOC-06) |
| **File** | `docs/ACCEPTANCE_REJECTION_FLOW.md` |

Key invariants:
- Every rejected candidate MUST have a classified `rejection_reason`
- No terminal no-output state without a reason chain
- Provider failures (rate limits, timeouts) MUST NOT contaminate task success metrics

---

#### DOC-08: Workspace & Configuration Reference

| Field | Value |
|---|---|
| **Purpose** | Complete reference for workspace layout, config files, and schemas |
| **Audience** | Users configuring audits, developers debugging workspace issues |
| **Modules covered** | Workspace directory structure, `config/policies.yaml`, `rules/transition_rules.yaml`, `prompts/*`, `schema/*` |
| **Required sections** | Workspace directory tree / audit_config.json fields / policies.yaml reference / transition_rules.yaml reference / Worker prompt templates / JSON schema index |
| **Can skip** | Runtime internals that produce these artifacts |
| **File** | `docs/CONFIGURATION.md` |

---

### P2 — Long-Tail Docs

#### DOC-09: Contributing Guide

| Field | Value |
|---|---|
| **Purpose** | Onboarding for external contributors |
| **Audience** | Open-source contributors |
| **Modules covered** | Dev setup, test running, PR process |
| **Required sections** | Dev environment / Running tests / Code style / PR checklist |
| **Can skip** | Architecture (reference DOC-02) |
| **File** | `CONTRIBUTING.md` |

---

#### DOC-10: Schema Reference

| Field | Value |
|---|---|
| **Purpose** | Human-readable documentation for all JSON schemas |
| **Audience** | Integrators consuming DAR output programmatically |
| **Modules covered** | `schema/*.json` |
| **Required sections** | Per-schema field reference / Version history / Cross-references between schemas |
| **Can skip** | Validation implementation details |
| **File** | `docs/SCHEMA_REFERENCE.md` |

---

#### DOC-11: Changelog

| Field | Value |
|---|---|
| **Purpose** | Version history |
| **Audience** | All users |
| **Modules covered** | N/A |
| **Required sections** | Keep-a-changelog format |
| **Can skip** | — |
| **File** | `CHANGELOG.md` |

---

## Code Hotspots — Inline Documentation Needed

These modules have complex invariants or non-obvious semantics that require
docstrings and inline comments. Existing docstrings range from good to absent.

### Critical (P0)

| Module | Lines | What to document |
|---|---|---|
| `runtime/processing.py` | 529 | Main orchestrator. Document the `process_candidate_events` pipeline: accept/reject decision tree, system-acceptor semantics, tracing context. This is the hardest module to understand. |
| `runtime/canonicalization.py` | 928 | Document event type registry, entity ID generation (hash-based), normalization rules (unicode, whitespace), schema version contract. Many constants without explanation. |
| `runtime/projector.py` | ~350 | Document projection algorithm: event replay order, entity collection merging, deterministic hash for projection_id, relationship to snapshot. Core invariant: same events → same state. |
| `runtime/adapters/base.py` | 597 | OutcomeLevel enum has good docs already. Fill gaps in: BackendAdapter protocol methods, WorkerInput/WorkerOutput contracts, transport envelope format. |
| `cli.py` | 1140 | Document the CLI arg groupings, workspace initialization flow, the sequence: snapshot → task plan → slice → process → compile report. |

### High (P1)

| Module | Lines | What to document |
|---|---|---|
| `runtime/rejection.py` | 500 | Good top-level docstring exists. Add: per-RejectionReason examples, rejection chain assembly logic, relationship to outcome.py. |
| `runtime/outcome.py` | 519 | Good docstrings on enums. Add: OutcomeClassifier decision tree, provider isolation invariant, how outcomes map to run_ledger entries. |
| `runtime/tasks.py` | 1479 | Document TaskPlanner algorithm, task status lifecycle, task ID generation, queue persistence, slice-to-task mapping. Large module with minimal docs. |
| `runtime/adapters/selector.py` | ~200 | Good docstring exists. Minor: document selection fallback guarantees (NO implicit fallback). |
| `runtime/report_compiler.py` | 762 | Document report compilation algorithm, audit status lifecycle, observation→finding mapping, candidate appendix generation. |
| `runtime/validators/transition.py` | ~300 | Document transition matrix evaluation, candidate layer augmentation, worker permission model. |
| `runtime/event_store.py` | 553 | Document event ID computation, idempotency key semantics, filesystem lock protocol, atomic write contract. |
| `runtime/pattern_scanner.py` | ~250 | Good docstring exists. Add: per-category regex explanation, confidence scoring, integration point with Reader worker. |

### Medium (P2)

| Module | Lines | What to document |
|---|---|---|
| `runtime/run_ledger.py` | 925 | Document NDJSON run ledger format, worker execution trace, timing fields. |
| `runtime/slice_builder.py` | 963 | Document slice semantics, memory budget model, target resolution. |
| `runtime/repair/repairer.py` | 687 | Document repair strategies, entity_type_mapping logic, when repair is attempted vs rejected. |
| `runtime/workers/*.py` | ~1800 total | Each worker has a prompt template; document the input/output contract per worker and how raw LLM output is parsed. |
| `runtime/evidence.py` | ~200 | Document evidence class taxonomy, schema v1.3 changes, finding-level evidence requirements. |
| `runtime/secret_redaction.py` | ~200 | Document redaction patterns, what is and isn't redacted, redaction in reports vs events. |
| `runtime/coverage_tracker.py` | ~200 | Document coverage model, file-level tracking, reporting. |
| `runtime/diagnostics.py` | 716 | Document diagnostic modes, output format, when diagnostics are emitted. |

---

## What Does NOT Need Separate Documentation

These areas are adequately covered by code + tests and don't warrant standalone docs:

- **`runtime/validators/models.py`** — simple dataclasses, self-documenting
- **`runtime/validators/semantic_content.py`** — straightforward checks
- **`runtime/validators/duplicate.py`** — ID-based dedup, clear from code
- **`runtime/snapshot.py`** — file hashing, clear purpose
- **`runtime/context_redaction.py`** — complements secret_redaction
- **`runtime/failure_artifacts.py`** — JSON dump utility
- **`runtime/file_prioritization.py`** — ordering heuristics, clear from tests
- **`runtime/validators/source_binding.py`** — source ref validation
- **Individual test files** — well-named, serve as documentation

---

## Summary

| Priority | Docs | Effort |
|---|---|---|
| **P0** | README rewrite (DOC-01), Architecture (DOC-02), Getting Started (DOC-03), 5 code hotspots | ~3 focused sessions |
| **P1** | Adapter Boundary (DOC-04), Event Sourcing (DOC-05), Validation Pipeline (DOC-06), Acceptance/Rejection (DOC-07), Configuration (DOC-08), 8 code hotspots | ~5 focused sessions |
| **P2** | Contributing (DOC-09), Schema Reference (DOC-10), Changelog (DOC-11), 8 code hotspots | ~3 focused sessions |

**Total documentation surface**: 11 documents + 21 code hotspots.
This is a **medium** documentation effort for a project of this complexity — the codebase is well-structured and many modules are self-documenting, but the pipeline invariants and event-sourcing model require explicit explanation.
