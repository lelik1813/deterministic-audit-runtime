# Explicit Non-Goals

**Status:** Documentation
**Date:** 2026-03-25
**Audience:** All Stakeholders

---

## Purpose

This document explicitly states what the Claude SDK integration **does NOT** do. Understanding these boundaries is critical for setting correct expectations and preventing architectural drift.

---

## Core Non-Goals

### 1. Claude is NOT a System Orchestrator

**What we're NOT doing:**
- Giving Claude control over task routing
- Letting Claude decide which workers to invoke
- Allowing Claude to manage the audit lifecycle
- Delegating workflow decisions to Claude

**What Claude DOES:**
- Execute single worker invocations as bounded tasks
- Return structured output for outer runtime to process
- Operate within strict policy envelope

**Why this matters:**
The outer runtime remains the single source of truth for orchestration. Claude is a worker backend, not a controller.

```text
❌ WRONG: Claude decides what to do next
✅ RIGHT: Runtime decides, Claude executes bounded task
```

---

### 2. NO MCP Client/Server Model

**What we're NOT doing:**
- Implementing MCP (Model Context Protocol) server
- Converting runtime to MCP client
- Exposing runtime capabilities via MCP
- Building Claude as MCP orchestrator

**What we HAVE:**
- Backend adapter protocol (internal)
- Policy envelope (internal)
- Capability negotiation (internal)

**Why this matters:**
MCP is a separate architectural decision. This integration uses Claude SDK directly, not through MCP abstraction.

```text
❌ WRONG: Runtime ←→ MCP Server ←→ Claude
✅ RIGHT: Runtime → BackendAdapter → Claude SDK
```

---

### 3. NO Unrestricted Shell/Web/File Access

**What we're NOT doing:**
- Giving Claude free shell access
- Allowing unrestricted file system writes
- Permitting arbitrary network requests
- Enabling "Claude can do anything" mode

**What we HAVE:**
- Deny-by-default policy envelope
- Explicit capability grants
- Allowed roots for file access
- Domain allowlists for web access

**Why this matters:**
Security is non-negotiable. Every capability must be explicitly granted.

```text
❌ WRONG: Claude can run any command
✅ RIGHT: Claude can only run commands in allowlist (if any)
```

---

### 4. NO Direct Writes to Canonical State

**What we're NOT doing:**
- Letting Claude emit events directly
- Allowing Claude to modify event store
- Permitting Claude to update projector state
- Bypassing validation for Claude output

**What we HAVE:**
- Claude produces candidate events
- Outer runtime validates candidates
- Runtime decides what to commit
- All mutations go through deterministic path

**Why this matters:**
The event store is append-only and immutable. Claude's output is always a *proposal*, not a *command*.

```text
❌ WRONG: Claude → Event Store
✅ RIGHT: Claude → Adapter → Validation → Runtime → Event Store
```

---

### 5. NO Hidden Persistent Conversational Memory

**What we're NOT doing:**
- Maintaining session state between invocations
- Carrying conversation history across worker runs
- Allowing Claude to "remember" previous tasks
- Building persistent agent memory

**What we HAVE:**
- Each invocation is independent
- No conversation history passed
- Context is explicit in invocation bundle
- Fresh session for every worker run

**Why this matters:**
Determinism requires isolation. Replayability requires stateless invocations.

```text
❌ WRONG: Invocation 2 remembers Invocation 1
✅ RIGHT: Invocation 2 starts with no memory of Invocation 1
```

---

### 6. NO Replacement of Deterministic Verification

**What we're NOT doing:**
- Trusting Claude output without validation
- Bypassing schema validation for Claude
- Skipping reference resolution checks
- Ignoring evidence binding verification

**What we HAVE:**
- Same validation pipeline for all backends
- Schema validation on every output
- Reference resolution required
- Evidence binding verified

**Why this matters:**
Claude can hallucinate. Validation catches hallucinations before they affect state.

```text
❌ WRONG: Claude says X → X is true
✅ RIGHT: Claude says X → validate(X) → X is true (or rejected)
```

---

### 7. NO Autonomous Agent Loops

**What we're NOT doing:**
- Allowing Claude to loop indefinitely
- Giving Claude authority to spawn tasks
- Permitting Claude to call other workers
- Enabling self-orchestrating agent behavior

**What we HAVE:**
- Bounded agent turns (max 10)
- Tool call budget (max 50)
- Duration budget (max 60s)
- Hard fail on budget exhaustion

**Why this matters:**
Cost and safety require bounded execution. Claude cannot run forever.

```text
❌ WRONG: Claude decides when to stop
✅ RIGHT: Budget enforcer decides when Claude stops
```

---

### 8. NO Silent Fallbacks

**What we're NOT doing:**
- Automatically falling back to Codex on Claude failure
- Silently retrying with different backend
- Hiding backend errors from operators
- Masking cost differences

**What we HAVE:**
- Explicit failure classification
- Controller decides on retries
- Full observability on backend selection
- Audit trail of which backend was used

**Why this matters:**
Silent fallbacks hide problems. Different backends may produce different results.

```text
❌ WRONG: Claude fails → silently use Codex result
✅ RIGHT: Claude fails → report failure → controller decides
```

---

## Summary Table

| Non-Goal | What We Do Instead |
|----------|-------------------|
| Claude as orchestrator | Claude as bounded worker |
| MCP model | Backend adapter protocol |
| Unrestricted access | Policy envelope |
| Direct state writes | Candidate events |
| Persistent memory | Stateless invocations |
| Skip verification | Same validation pipeline |
| Autonomous loops | Budget-bounded execution |
| Silent fallbacks | Explicit failure handling |

---

## Future Considerations

These non-goals are **current** constraints. They may be revisited in future phases with appropriate safeguards:

| Potential Future Change | Requirements Before Reconsidering |
|------------------------|-----------------------------------|
| Session reuse | Formal replayability analysis |
| Limited shell access | Comprehensive security audit |
| MCP integration | Separate architectural decision |
| Multi-worker delegation | Clear authority boundaries |

Any relaxation of these non-goals requires:
1. Security review
2. Cost/budget analysis
3. Documentation update
4. Team consensus
5. Feature flag with gradual rollout

---

## Definition of Done

- [x] Claude explicitly not an orchestrator
- [x] No MCP model
- [x] No unrestricted access
- [x] No direct state writes
- [x] No persistent memory
- [x] No verification bypass
- [x] No autonomous loops
- [x] No silent fallbacks
- [x] Summary table provided
- [x] Future considerations documented

---

## Appendix: Anti-Patterns to Avoid

### Anti-Pattern 1: "Just Let Claude Handle It"

```python
# ❌ WRONG
def process_audit(audit_id):
    claude.invoke(f"Run the entire audit for {audit_id}")

# ✅ RIGHT
def process_audit(audit_id):
    for task in get_tasks(audit_id):
        worker = select_worker(task)
        result = invoke_backend(worker, task)
        validate_and_commit(result)
```

### Anti-Pattern 2: "Claude Can Figure Out the Policy"

```python
# ❌ WRONG
policy = BackendPolicyEnvelope.allow_all()  # "Claude knows best"

# ✅ RIGHT
policy = get_policy_for_role(worker_role, allowed_roots=[workspace])
```

### Anti-Pattern 3: "Remember What Claude Did Last Time"

```python
# ❌ WRONG
session = get_or_create_session(worker_id)  # Persistent session

# ✅ RIGHT
context = build_fresh_context(worker_input)  # No session reuse
```

### Anti-Pattern 4: "Trust Claude's Output"

```python
# ❌ WRONG
result = claude.invoke(prompt)
canonical_state.apply(result)  # No validation!

# ✅ RIGHT
result = claude.invoke(prompt)
candidates = normalize_and_validate(result)
canonical_state.commit(candidates)  # After validation
```

---

## Related Documentation

- [Claude Backend Architecture](./claude_backend_architecture.md) - What we ARE building
- [Security Review](./security_review.md) - Security constraints
- [Rollout Plan](./rollout_plan.md) - Phased approach
- [ADR-001](./adr/001-claude-sdk-integration-boundary.md) - Original decision record
