# Architectural Positioning

## Problem

LLM-based analysis systems treat model output as ground truth. This creates a class of failure where fabricated, hallucinated, or unverifiable content propagates into final deliverables without detection.

The root cause is architectural, not model-level:

- LLM output is treated as authoritative by default
- there is no verification layer between generation and acceptance
- claims are not bound to verifiable evidence
- no audit trail exists to trace how a conclusion was reached
- results are not reproducible — the same input produces different output on each run

This is not hypothetical. In 2025, a Deloitte report produced for the Australian government contained fabricated citations and nonexistent academic sources — delivered and billed before anyone could verify the references.

## Approach

DAR applies a verification-first execution model borrowed from classical cognitive architectures (ACT-R, Soar):

- **state and memory are external** — owned by the runtime, not implicit in the model
- **reasoning operates over explicit state** — the LLM does not control the data plane
- **transitions are governed by rules** — deterministic validation, not model judgement
- **progression proceeds through a controlled cycle** — bounded tasks, not open-ended generation

Concrete mechanism:

```
LLM produces candidate events
    → runtime normalizes to canonical form
    → deterministic validation pipeline accepts or rejects
    → accepted events append to immutable log
    → canonical state is projected from the log (never written directly)
```

The LLM never writes to state. It never decides what to do next. It never verifies its own output. It provides candidate hypotheses; the runtime validates, records, and decides.

## Guarantees

### What DAR guarantees

- **No unverified content reaches canonical state.** Every accepted event passes through the deterministic validation pipeline.
- **Every accepted fact has a validation trace.** The `acceptance` field on each event records who accepted it and why.
- **Canonical state is reproducible.** The same event log always produces the same state. `rebuild-state` can be run at any time.
- **No silent drops.** Every observation that does not become a finding is recorded in a suppression record with an explicit reason code.
- **Provider failures are isolated.** Backend errors and rate limits are classified separately from task correctness metrics. A flaky backend cannot make the audit appear broken.
- **Audit trail is immutable.** The event log is append-only. Events are never modified or deleted.

### What DAR does not guarantee

- **Completeness of findings.** DAR does not claim to find all security issues in a target repository. Coverage depends on scan targets, worker quality, and backend capability.
- **Factual correctness of individual observations.** Accepted observations pass structural validation (schema, source binding, transition rules). DAR does not verify that the semantic claim is true — only that it is well-formed and properly evidenced.
- **Determinism of backend output.** The LLM is non-deterministic. Two runs on the same code may produce different candidates. DAR constrains the effect: different candidates that fail validation are rejected; different candidates that pass validation produce different but equally valid states.
- **Stability of schemas or APIs.** DAR is in active development. Schema versions are tracked, but no API stability guarantees exist yet.
- **Performance or latency.** Execution time depends on backend response times, which are outside DAR's control.
