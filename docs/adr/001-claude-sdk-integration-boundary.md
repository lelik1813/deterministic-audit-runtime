# ADR-001: Claude Agent SDK Integration Boundary

**Status:** Accepted
**Date:** 2026-03-25
**Scope:** Claude Agent SDK integration as internal worker backend

---

## Decision 1: Claude Agent SDK selected over Claude Code CLI for internal worker execution

### Context
The deterministic runtime requires a bounded execution backend for worker invocations. Two options were considered:
- Claude Code CLI (subprocess-based, external process)
- Claude Agent SDK (in-process, programmable API)

### Decision
Use Claude Agent SDK as the internal worker backend.

### Rationale
- **Programmatic control**: SDK provides direct API integration vs subprocess orchestration
- **Better error handling**: Exceptions and structured errors vs parsing CLI output
- **Streaming support**: Native streaming API vs stdout parsing
- **Testability**: Easier to mock/inject vs spawning external processes
- **Latency**: No subprocess startup overhead

---

## Decision 2: Agent SDK is used as programmable sub-agent, not as autonomous system controller

### Context
Claude SDK could theoretically be used as an orchestrator or autonomous agent.

### Decision
Claude SDK is strictly a **bounded sub-agent**. It has no autonomous authority.

### Constraints
- Claude SDK receives a single worker invocation request
- Claude SDK returns a single normalized result
- Claude SDK cannot spawn additional tasks
- Claude SDK cannot modify runtime state directly
- Claude SDK cannot make routing decisions

### Rationale
The outer deterministic runtime must remain the single source of control. Claude provides reasoning and output generation, not orchestration.

---

## Decision 3: Outer runtime remains authoritative for state, policy, retries, validation, and commit

### Context
Integration must not compromise the deterministic guarantees of the existing runtime.

### Decision
The outer runtime retains exclusive authority over:

| Responsibility | Claude SDK | Outer Runtime |
|----------------|------------|---------------|
| Event store writes | Denied | Authoritative |
| Projector mutation | Denied | Authoritative |
| Task state transitions | Denied | Authoritative |
| Retry policy | Denied | Authoritative |
| Worker routing | Denied | Authoritative |
| Final issue acceptance | Denied | Authoritative |
| Policy enforcement | Denied | Authoritative |
| Output validation | Denied | Authoritative |

### Allowed Output Path
```
Claude SDK result
  -> adapter normalization
  -> transport validation
  -> worker schema validation
  -> candidate events
  -> deterministic verifier / controller
  -> (only then) commit
```

---

## Must-Preserve Properties

The following invariants must be preserved at all times during integration:

### 1. Deterministic Replay at Runtime Level
- Every worker invocation must be replayable from event history
- Claude SDK invocations must be fully described by invocation input + policy envelope
- No hidden state that affects replay

### 2. Append-Only Event Discipline
- Events are only appended, never mutated
- Claude SDK cannot modify existing events
- Claude SDK output becomes candidate events subject to validation

### 3. Evidence-Binding
- All worker outputs must bind to evidence
- Claude SDK must reference evidence from input, not hallucinate
- Evidence provenance is tracked and validated

### 4. No Direct Mutation from Model Backend
- Claude SDK writes nothing directly to canonical state
- All outputs go through adapter normalization
- Controller decides what gets committed

### 5. Schema-First Worker Outputs
- Every worker has a defined output schema
- Claude SDK output must conform to schema or be rejected
- Partial/invalid outputs are not accepted

### 6. Policy Before Side Effects
- Policy envelope is evaluated before any tool execution
- Denied operations fail fast, not during execution
- No side effects bypass policy

---

## Architecture Boundary Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    CLI / Runtime Controller                  │
│                  (single source of authority)                │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    Worker Orchestrator                       │
│     (task lifecycle, retries, event emission, validation)    │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                     BackendAdapter                           │
│              (backend selection, normalization)              │
└───────────┬─────────────────────────────────────┬───────────┘
            │                                     │
            ▼                                     ▼
┌───────────────────────┐             ┌───────────────────────┐
│    CodexAdapter       │             │  ClaudeAgentSdkAdapter │
│   (subprocess CLI)    │             │   (in-process SDK)     │
│                       │             │                         │
│  - bounded execution  │             │  - bounded execution    │
│  - no authority       │             │  - no authority         │
│  - output normalized  │             │  - output normalized    │
└───────────────────────┘             └───────────────────────┘
```

---

## Consequences

### Positive
- Clear separation of concerns
- Deterministic guarantees preserved
- Testable integration points
- Gradual rollout possible

### Negative
- Additional abstraction layer
- More complex adapter implementation
- Need for comprehensive conformance tests

### Risks
- SDK behavior changes could affect integration
- Must maintain tight policy envelope

---

## References
- TODO_Claude_SDK.md Step 0
- docs/architecture_overview.md
- docs/codex_adapter.md
