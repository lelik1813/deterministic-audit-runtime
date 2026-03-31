# v1.2 Preserved Invariants

## Purpose

This document restates all invariants preserved from AGENTS.md and v1.1, with explicit
mapping to what each invariant protects and why it must not change in v1.2.

## Fundamental Invariants

### 1. Source of Truth

**Invariant**: Canonical truth exists ONLY in `/events` and `/state`.

**What It Protects**: Prevents drift where conversation history, worker memory,
or markdown prose become de facto truth sources.

**Why It Must Not Change**: If truth can exist outside the event log, audit
reproducibility is lost. v1.2's candidate layer, run ledger, and secret redaction
are NOT truth sources — they are observability and security layers only.

**v1.2 Implication**:
- Run ledger entries are trace records, not canonical state
- Redacted strings are persistence artifacts, not authoritative content
- Candidate events are proposals, not accepted facts

---

### 2. Execution Model

**Invariant**: Workers operate as constrained executors following READ → VALIDATE → EMIT → STOP.

**What It Protects**: Prevents workers from accumulating implicit state or
making unauthorized decisions.

**Why It Must Not Change**: If workers become autonomous reasoners, the audit
process becomes non-deterministic and untraceable.

**v1.2 Implication**:
- Deterministic task identity does not grant workers autonomy
- Secret redaction does not grant workers access to redacted content
- Run ledger recording does not feed back into worker decisions

---

### 3. State Interaction Rules

**Invariant**: Workers MUST treat `/events` as append-only and `/state` as derived.
Workers MUST NOT mutate canonical state directly.

**What It Protects**: Ensures all state changes go through the event pipeline
where validation occurs.

**Why It Must Not Change**: Direct state mutation bypasses validation and
breaks the audit trail.

**v1.2 Implication**:
- Secret redaction happens at serialization, not during state derivation
- Run ledger is append-only and never modifies canonical state
- Task identity generation is deterministic but does not mutate state

---

### 4. Worker Role Constraints

**Invariant**: Workers operate in explicit roles ONLY (Reader, Verifier, IssueComposer).
No implicit or mixed roles are allowed.

**What It Protects**: Prevents unauthorized actions like issue creation during
reading or severity assignment during verification.

**Why It Must Not Change**: Role confusion leads to invalid audit conclusions
where unverified claims become findings.

**v1.2 Implication**:
- v1.2 adds no new worker roles
- Candidate generation follows existing role constraints
- Run ledger records role context but does not modify role definitions

---

### 5. Source Binding (Critical Invariant)

**Invariant**: Every fact MUST include file path, line range, and snapshot reference.

**What It Protects**: Ensures all claims can be verified against specific
evidence in the target repository.

**Why It Must Not Change**: Facts without source binding cannot be verified
or reproduced. They become unsupported assertions.

**v1.2 Implication**:
- Secret redaction preserves source binding metadata
- Candidate events require source binding before acceptance
- Run ledger traces include source digest references

**If source binding is missing**: DO NOT emit fact → emit hypothesis or question instead.

---

### 6. Output Contract

**Invariant**: Workers MUST produce structured outputs only, following defined schemas.

**What It Protects**: Ensures machine-parseable outputs that can be validated
and projected deterministically.

**Why It Must Not Change**: Free-form output cannot be validated, projected,
or compiled into reports.

**v1.2 Implication**:
- Secret redaction operates on structured output, not prose
- Run ledger entries are structured JSON, not narrative
- Task identity is derived from structured task attributes

---

### 7. Forbidden Behaviors

**Invariant**: Workers MUST NOT:
- Rely on earlier conversation unless it is in state
- Summarize state as memory
- Merge facts without explicit linkage
- Assume correctness without validation
- Generate conclusions without verified evidence
- Silently upgrade hypothesis to fact
- Create issues without evidence graph

**What It Protects**: Prevents the most common failure modes in audit
integrity — drift, assumption, and unauthorized promotion.

**Why It Must Not Change**: Each forbidden behavior represents a historical
failure that compromised an audit.

**v1.2 Implication**:
- v1.2 does not introduce new exception cases
- Secret redaction is transparent to validation logic
- Run ledger does not create new fact sources

---

### 8. Step Discipline

**Invariant**: Workers MUST execute ONLY the requested step. No skipping ahead,
no combining steps, no completing multiple TODO steps at once.

**What It Protects**: Prevents workers from making assumptions about what
should happen next, which leads to premature conclusions.

**Why It Must Not Change**: Audit quality depends on methodical, verified
progression through discrete steps.

**v1.2 Implication**:
- Deterministic task identity does not enable parallel step execution
- Run ledger records step context but does not drive step selection

---

### 9. Determinism Preference

**Invariant**: The system SHOULD prefer deterministic structures over flexible ones,
explicit schemas over implicit formats, reproducibility over convenience.

**What It Protects**: Ensures audit results can be reproduced and verified
by independent parties.

**Why It Must Not Change**: Non-deterministic systems cannot be audited
or trusted.

**v1.2 Implication**:
- Task identity is deterministic by design (SHA256 hash)
- Secret redaction uses deterministic markers (hash-based)
- Run ledger entries are deterministically ordered

---

### 10. Anti-Drift Rules

**Invariant**: If uncertain: emit question or hypothesis. DO NOT guess,
fill gaps implicitly, or smooth inconsistencies.

**What It Protects**: Ensures all uncertainty is explicit and traceable.

**Why It Must Not Change**: Hidden uncertainty becomes false confidence
in audit conclusions.

**v1.2 Implication**:
- Candidates with uncertainty remain candidates until resolved
- Secret redaction does not mask uncertainty
- Run ledger preserves uncertainty indicators

---

## v1.2-Specific Prohibitions

### Candidates Are Never Truth-Bearing Entities

**Statement**: Candidate events, candidate observations, and candidate issues
are proposals, not facts.

**Protected Invariant**: Source of Truth (Invariant 1)

**Reasoning**: If candidates could be treated as truth, the validation pipeline
would be bypassed and unverified claims would enter reports.

**Implementation**:
- Candidates exist only in the event stream before acceptance
- Reports compile from canonical state only (accepted events)
- Run ledger traces candidates but does not promote them

---

### Direct Candidate → Issue Path Is Prohibited

**Statement**: No direct path exists from a candidate event to an issue.

**Protected Invariants**: Source Binding (Invariant 5), Worker Role Constraints (Invariant 4)

**Reasoning**: Issues must be composed from verified observations. A candidate
observation must pass through validation and be accepted before it can
contribute to an issue.

**Required Flow**:
```
candidate observation → validation → accepted observation → issue composition
```

**Prohibited Flow**:
```
candidate observation → issue (DIRECT - PROHIBITED)
```

**Implementation**:
- IssueComposer role can only reference observations in canonical state
- Canonical state is projected from accepted events only
- Candidate observations not in canonical state are invisible to IssueComposer

---

## Invariant Mapping Summary

| Invariant | Protects | v1.2 Impact |
|-----------|----------|-------------|
| Source of Truth | Reproducibility | Candidate layer is non-authoritative |
| Execution Model | Determinism | Workers remain constrained |
| State Interaction | Audit trail | Redaction at serialization only |
| Role Constraints | Authorization | No new roles in v1.2 |
| Source Binding | Verifiability | Binding preserved through redaction |
| Output Contract | Machine validation | Structured output required |
| Forbidden Behaviors | Audit integrity | No new exceptions |
| Step Discipline | Methodical progress | Single-step execution required |
| Determinism | Reproducibility | Task identity is hash-based |
| Anti-Drift | Explicit uncertainty | Uncertainty preserved |

---

## Verification Checklist

v1.2 invariants are preserved when:

- [ ] No candidate appears in compiled reports
- [ ] No issue references a non-accepted observation
- [ ] Run ledger entries never modify canonical state
- [ ] Secret redaction never weakens validation
- [ ] Task identity remains deterministic and idempotent
- [ ] All v1.1 tests pass without modification
- [ ] Workers cannot access redacted secret content
- [ ] Source binding is present in all accepted facts
