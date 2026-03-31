"""
Capability Model for Backend Adapters.

This module defines the capability matrix and negotiation mechanism for
backend adapters. Capabilities are divided into:

1. Declared capabilities: what the backend claims to support
2. Runtime-allowed capabilities: subset allowed by policy envelope

Negotiation flow:
1. Worker declares required capabilities
2. Backend provides declared capabilities
3. Policy envelope restricts to allowed subset
4. Negotiation fails fast if requirements cannot be met
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# =============================================================================
# Capability Definitions
# =============================================================================

class CapabilityKind(Enum):
    """All known capability kinds for backend adapters."""

    # Execution capabilities
    SESSION_CONTEXT = "supports_session_context"
    """Can maintain context across multiple invocations."""

    AGENT_LOOP = "supports_agent_loop"
    """Can perform multi-turn reasoning with tool use."""

    STREAMING = "supports_streaming"
    """Can stream output incrementally."""

    # Tool capabilities
    FILE_READ = "supports_file_read"
    """Can read files from filesystem."""

    FILE_WRITE = "supports_file_write"
    """Can write files to filesystem."""

    SHELL = "supports_shell"
    """Can execute shell commands."""

    WEB = "supports_web"
    """Can make network/web requests."""

    # Output capabilities
    STRUCTURED_OUTPUT_ENFORCEMENT = "supports_structured_output_enforcement"
    """Can enforce structured output via schema."""

    TOOL_RESTRICTION = "supports_tool_restriction"
    """Can restrict which tools are available."""

    MODEL_OVERRIDE = "supports_model_override"
    """Can override the model being used."""

    # Additional capabilities
    TOOL_LOOP = "supports_tool_loop"
    """Can execute tool calling loops."""

    PARALLEL_TOOLS = "supports_parallel_tools"
    """Can execute multiple tools in parallel."""

    SESSION_PERSISTENCE = "supports_session_persistence"
    """Can persist and restore session state."""

    JSON_MODE = "supports_json_mode"
    """Can enforce JSON output mode."""

    TEMPERATURE_CONTROL = "supports_temperature_control"
    """Can control model temperature."""

    MAX_TOKENS_CONTROL = "supports_max_tokens_control"
    """Can control max output tokens."""


@dataclass(frozen=True)
class CapabilityDefinition:
    """Definition of a single capability."""
    kind: CapabilityKind
    """The capability kind."""

    name: str
    """Human-readable name."""

    description: str
    """Description of what this capability enables."""

    risk_level: str = "low"
    """Risk level: low, medium, high."""

    requires_policy_allow: bool = True
    """Whether this capability requires explicit policy allowance."""

    default_allowed: bool = False
    """Whether this capability is allowed by default (without policy)."""


# =============================================================================
# Capability Registry
# =============================================================================

CAPABILITY_DEFINITIONS: dict[CapabilityKind, CapabilityDefinition] = {
    # Execution capabilities
    CapabilityKind.SESSION_CONTEXT: CapabilityDefinition(
        kind=CapabilityKind.SESSION_CONTEXT,
        name="Session Context",
        description="Can maintain context across multiple invocations",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.AGENT_LOOP: CapabilityDefinition(
        kind=CapabilityKind.AGENT_LOOP,
        name="Agent Loop",
        description="Can perform multi-turn reasoning with tool use",
        risk_level="medium",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.STREAMING: CapabilityDefinition(
        kind=CapabilityKind.STREAMING,
        name="Streaming Output",
        description="Can stream output incrementally",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),

    # Tool capabilities
    CapabilityKind.FILE_READ: CapabilityDefinition(
        kind=CapabilityKind.FILE_READ,
        name="File Read",
        description="Can read files from filesystem",
        risk_level="medium",
        requires_policy_allow=True,
        default_allowed=False,
    ),
    CapabilityKind.FILE_WRITE: CapabilityDefinition(
        kind=CapabilityKind.FILE_WRITE,
        name="File Write",
        description="Can write files to filesystem",
        risk_level="high",
        requires_policy_allow=True,
        default_allowed=False,
    ),
    CapabilityKind.SHELL: CapabilityDefinition(
        kind=CapabilityKind.SHELL,
        name="Shell Execution",
        description="Can execute shell commands",
        risk_level="high",
        requires_policy_allow=True,
        default_allowed=False,
    ),
    CapabilityKind.WEB: CapabilityDefinition(
        kind=CapabilityKind.WEB,
        name="Web Access",
        description="Can make network/web requests",
        risk_level="high",
        requires_policy_allow=True,
        default_allowed=False,
    ),

    # Output capabilities
    CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT: CapabilityDefinition(
        kind=CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
        name="Structured Output Enforcement",
        description="Can enforce structured output via schema",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.TOOL_RESTRICTION: CapabilityDefinition(
        kind=CapabilityKind.TOOL_RESTRICTION,
        name="Tool Restriction",
        description="Can restrict which tools are available",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.MODEL_OVERRIDE: CapabilityDefinition(
        kind=CapabilityKind.MODEL_OVERRIDE,
        name="Model Override",
        description="Can override the model being used",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),

    # Additional capabilities
    CapabilityKind.TOOL_LOOP: CapabilityDefinition(
        kind=CapabilityKind.TOOL_LOOP,
        name="Tool Loop",
        description="Can execute tool calling loops",
        risk_level="medium",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.PARALLEL_TOOLS: CapabilityDefinition(
        kind=CapabilityKind.PARALLEL_TOOLS,
        name="Parallel Tools",
        description="Can execute multiple tools in parallel",
        risk_level="medium",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.SESSION_PERSISTENCE: CapabilityDefinition(
        kind=CapabilityKind.SESSION_PERSISTENCE,
        name="Session Persistence",
        description="Can persist and restore session state",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.JSON_MODE: CapabilityDefinition(
        kind=CapabilityKind.JSON_MODE,
        name="JSON Mode",
        description="Can enforce JSON output mode",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.TEMPERATURE_CONTROL: CapabilityDefinition(
        kind=CapabilityKind.TEMPERATURE_CONTROL,
        name="Temperature Control",
        description="Can control model temperature",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),
    CapabilityKind.MAX_TOKENS_CONTROL: CapabilityDefinition(
        kind=CapabilityKind.MAX_TOKENS_CONTROL,
        name="Max Tokens Control",
        description="Can control max output tokens",
        risk_level="low",
        requires_policy_allow=False,
        default_allowed=True,
    ),
}


# =============================================================================
# Worker Role Requirements
# =============================================================================

WORKER_ROLE_CAPABILITY_REQUIREMENTS: dict[str, set[CapabilityKind]] = {
    "Reader": {
        CapabilityKind.FILE_READ,
        CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
    },
    "Verifier": {
        CapabilityKind.FILE_READ,
        CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
    },
    "IssueComposer": {
        CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
        # No file access needed - works with provided context
    },
    "CandidateGenerator": {
        CapabilityKind.FILE_READ,
        CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
        CapabilityKind.AGENT_LOOP,  # May need multi-turn reasoning
    },
}


# =============================================================================
# Capability Matrix
# =============================================================================

@dataclass
class CapabilityMatrix:
    """
    Complete capability matrix for a backend.

    Tracks:
    - Declared capabilities: what the backend claims to support
    - Policy restrictions: what the policy envelope allows
    - Effective capabilities: intersection of declared and policy-allowed
    """

    backend_type: str
    """Backend type identifier."""

    declared_capabilities: set[CapabilityKind]
    """Capabilities the backend claims to support."""

    policy_allowed_capabilities: set[CapabilityKind] | None = None
    """Capabilities allowed by policy envelope (None = all declared allowed)."""

    def get_effective_capabilities(self) -> set[CapabilityKind]:
        """
        Get the effective capabilities (intersection of declared and allowed).

        If no policy restrictions are set, returns all declared capabilities.
        """
        if self.policy_allowed_capabilities is None:
            return self.declared_capabilities
        return self.declared_capabilities & self.policy_allowed_capabilities

    def has_capability(self, kind: CapabilityKind) -> bool:
        """Check if a capability is in the effective set."""
        return kind in self.get_effective_capabilities()

    def get_missing_capabilities(
        self,
        required: set[CapabilityKind],
    ) -> set[CapabilityKind]:
        """Get capabilities required but not in effective set."""
        effective = self.get_effective_capabilities()
        return required - effective

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "backend_type": self.backend_type,
            "declared_capabilities": [c.value for c in self.declared_capabilities],
            "policy_allowed_capabilities": (
                [c.value for c in self.policy_allowed_capabilities]
                if self.policy_allowed_capabilities else None
            ),
            "effective_capabilities": [c.value for c in self.get_effective_capabilities()],
        }


# =============================================================================
# Negotiation
# =============================================================================

@dataclass
class NegotiationResult:
    """Result of capability negotiation."""

    success: bool
    """Whether negotiation succeeded."""

    effective_capabilities: set[CapabilityKind]
    """Effective capabilities after negotiation."""

    missing_capabilities: set[CapabilityKind]
    """Capabilities required but not available."""

    negotiation_log: list[str] = field(default_factory=list)
    """Log of negotiation steps."""

    backend_type: str | None = None
    """Backend type that was negotiated with."""

    worker_role: str | None = None
    """Worker role that was being negotiated for."""


class CapabilityNegotiator:
    """
    Negotiates capabilities between worker requirements and backend availability.

    Negotiation flow:
    1. Worker declares required capabilities (via role mapping)
    2. Backend provides declared capabilities
    3. Policy envelope restricts to allowed subset
    4. Negotiation succeeds if all required capabilities are in effective set
    """

    def __init__(
        self,
        capability_definitions: dict[CapabilityKind, CapabilityDefinition] | None = None,
        worker_requirements: dict[str, set[CapabilityKind]] | None = None,
    ):
        self.definitions = capability_definitions or CAPABILITY_DEFINITIONS
        self.worker_requirements = worker_requirements or WORKER_ROLE_CAPABILITY_REQUIREMENTS

    def negotiate(
        self,
        worker_role: str,
        declared_capabilities: set[CapabilityKind],
        policy_allowed_capabilities: set[CapabilityKind] | None = None,
        backend_type: str = "unknown",
    ) -> NegotiationResult:
        """
        Negotiate capabilities for a worker invocation.

        Args:
            worker_role: Role of the worker being invoked
            declared_capabilities: Capabilities the backend declares
            policy_allowed_capabilities: Capabilities allowed by policy (None = all)
            backend_type: Backend type for logging

        Returns:
            NegotiationResult with success/failure and effective capabilities
        """
        log: list[str] = []

        # Step 1: Get worker requirements
        required = self.worker_requirements.get(worker_role, set())
        log.append(f"Worker role '{worker_role}' requires: {[c.value for c in required]}")
        log.append(f"Backend '{backend_type}' declares: {[c.value for c in declared_capabilities]}")

        # Step 2: Build capability matrix
        matrix = CapabilityMatrix(
            backend_type=backend_type,
            declared_capabilities=declared_capabilities,
            policy_allowed_capabilities=policy_allowed_capabilities,
        )

        if policy_allowed_capabilities is not None:
            log.append(f"Policy allows: {[c.value for c in policy_allowed_capabilities]}")

        # Step 3: Get effective capabilities
        effective = matrix.get_effective_capabilities()
        log.append(f"Effective capabilities: {[c.value for c in effective]}")

        # Step 4: Check if all required capabilities are available
        missing = required - effective

        if missing:
            log.append(f"NEGOTIATION FAILED: Missing capabilities: {[c.value for c in missing]}")
            # Add detailed failure reasons
            for cap in missing:
                if cap not in declared_capabilities:
                    log.append(f"  - {cap.value}: Not declared by backend")
                elif policy_allowed_capabilities is not None and cap not in policy_allowed_capabilities:
                    log.append(f"  - {cap.value}: Denied by policy")
        else:
            log.append("NEGOTIATION SUCCEEDED: All required capabilities available")

        return NegotiationResult(
            success=len(missing) == 0,
            effective_capabilities=effective,
            missing_capabilities=missing,
            negotiation_log=log,
            backend_type=backend_type,
            worker_role=worker_role,
        )

    def get_capability_definition(
        self,
        kind: CapabilityKind,
    ) -> CapabilityDefinition | None:
        """Get the definition for a capability kind."""
        return self.definitions.get(kind)

    def get_worker_requirements(
        self,
        worker_role: str,
    ) -> set[CapabilityKind]:
        """Get required capabilities for a worker role."""
        return self.worker_requirements.get(worker_role, set())


# =============================================================================
# Backend-Specific Capability Declarations
# =============================================================================

def get_codex_declared_capabilities(sandbox_mode: str = "read-only") -> set[CapabilityKind]:
    """
    Get declared capabilities for Codex backend based on sandbox mode.

    Args:
        sandbox_mode: Sandbox mode ("read-only" or "full-access")

    Returns:
        Set of declared capabilities
    """
    # Base capabilities always available
    base = {
        CapabilityKind.SESSION_CONTEXT,
        CapabilityKind.AGENT_LOOP,
        CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
        CapabilityKind.TOOL_RESTRICTION,
        CapabilityKind.MODEL_OVERRIDE,
        CapabilityKind.TOOL_LOOP,
        CapabilityKind.JSON_MODE,
    }

    # Capabilities based on sandbox mode
    if sandbox_mode == "read-only":
        return base | {CapabilityKind.FILE_READ}
    else:  # full-access
        return base | {
            CapabilityKind.FILE_READ,
            CapabilityKind.FILE_WRITE,
            CapabilityKind.SHELL,
        }


def get_claude_sdk_declared_capabilities() -> set[CapabilityKind]:
    """
    Get declared capabilities for Claude Agent SDK backend.

    Claude SDK supports more capabilities than Codex CLI.

    Returns:
        Set of declared capabilities
    """
    return {
        # Execution capabilities
        CapabilityKind.SESSION_CONTEXT,
        CapabilityKind.AGENT_LOOP,
        CapabilityKind.STREAMING,
        CapabilityKind.TOOL_LOOP,
        CapabilityKind.PARALLEL_TOOLS,
        CapabilityKind.SESSION_PERSISTENCE,

        # Tool capabilities (controlled by policy)
        CapabilityKind.FILE_READ,
        CapabilityKind.FILE_WRITE,
        CapabilityKind.SHELL,
        CapabilityKind.WEB,

        # Output capabilities
        CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
        CapabilityKind.TOOL_RESTRICTION,
        CapabilityKind.MODEL_OVERRIDE,
        CapabilityKind.JSON_MODE,
        CapabilityKind.TEMPERATURE_CONTROL,
        CapabilityKind.MAX_TOKENS_CONTROL,
    }


def policy_envelope_to_allowed_capabilities(
    allow_file_read: bool = False,
    allow_file_write: bool = False,
    allow_shell: bool = False,
    allow_web: bool = False,
) -> set[CapabilityKind]:
    """
    Convert policy envelope boolean flags to allowed capability set.

    Args:
        allow_file_read: Whether file read is allowed
        allow_file_write: Whether file write is allowed
        allow_shell: Whether shell execution is allowed
        allow_web: Whether web access is allowed

    Returns:
        Set of allowed capabilities based on policy
    """
    allowed: set[CapabilityKind] = {
        # Always allowed capabilities
        CapabilityKind.SESSION_CONTEXT,
        CapabilityKind.AGENT_LOOP,
        CapabilityKind.STREAMING,
        CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT,
        CapabilityKind.TOOL_RESTRICTION,
        CapabilityKind.MODEL_OVERRIDE,
        CapabilityKind.TOOL_LOOP,
        CapabilityKind.PARALLEL_TOOLS,
        CapabilityKind.SESSION_PERSISTENCE,
        CapabilityKind.JSON_MODE,
        CapabilityKind.TEMPERATURE_CONTROL,
        CapabilityKind.MAX_TOKENS_CONTROL,
    }

    # Conditional capabilities based on policy
    if allow_file_read:
        allowed.add(CapabilityKind.FILE_READ)
    if allow_file_write:
        allowed.add(CapabilityKind.FILE_WRITE)
    if allow_shell:
        allowed.add(CapabilityKind.SHELL)
    if allow_web:
        allowed.add(CapabilityKind.WEB)

    return allowed
