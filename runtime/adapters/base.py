"""
Backend Adapter Abstraction

This module defines the common interface and types for all worker execution backends.
All backends (Codex, Claude SDK, etc.) must implement the BackendAdapter protocol.

Key principle: Backends are bounded execution engines with NO authority over runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


# =============================================================================
# Outcome Levels
# =============================================================================

class OutcomeLevel(Enum):
    """
    Classification of backend operation outcomes.

    Outcomes are evaluated in order: each level must pass before the next is evaluated.
    Failure at any level terminates the invocation with an appropriate error.
    """

    # Level 1: Process outcome - did the backend process complete?
    PROCESS = "process"
    """
    Process outcome checks:
    - Did the backend executable/SDK initialize correctly?
    - Did the process complete without crash?
    - Was the process within resource limits (timeout, memory)?

    Failures at this level: backend not found, process crash, timeout
    """

    # Level 2: Transport outcome - was output received correctly?
    TRANSPORT = "transport"
    """
    Transport outcome checks:
    - Was output received (not empty)?
    - Is output valid JSON?
    - Does output match transport envelope schema?

    Failures at this level: empty output, invalid JSON, malformed envelope
    """

    # Level 3: Schema outcome - does output match worker schema?
    SCHEMA = "schema"
    """
    Schema outcome checks:
    - Does payload match worker-specific schema?
    - Are required fields present?
    - Are field types correct?

    Failures at this level: missing required fields, type mismatches
    """

    # Level 4: Semantic outcome - is output semantically valid?
    SEMANTIC = "semantic"
    """
    Semantic outcome checks:
    - Do entity references resolve?
    - Are claims internally consistent?
    - Is evidence properly bound?

    Failures at this level: invalid references, semantic contradictions
    """

    # Level 5: Policy outcome - does output comply with policy?
    POLICY = "policy"
    """
    Policy outcome checks:
    - Were only allowed tools used?
    - Were only allowed files accessed?
    - Was output within budget constraints?

    Failures at this level: policy violations, budget exceeded
    """


# =============================================================================
# Failure Taxonomy
# =============================================================================

class BackendFailureKind(Enum):
    """Classification of backend failures."""

    # Process-level failures
    BACKEND_UNAVAILABLE = "backend_unavailable"
    """Backend executable/SDK could not be found or initialized."""

    PROCESS_CRASH = "process_crash"
    """Backend process crashed during execution."""

    TIMEOUT = "timeout"
    """Backend execution exceeded time limit."""

    # Transport-level failures
    EMPTY_OUTPUT = "empty_output"
    """Backend returned no output."""

    INVALID_JSON = "invalid_json"
    """Backend output is not valid JSON."""

    MALFORMED_ENVELOPE = "malformed_envelope"
    """Output doesn't match transport envelope schema."""

    # Schema-level failures
    SCHEMA_VIOLATION = "schema_violation"
    """Output doesn't match worker-specific schema."""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    """Required field is missing from output."""

    TYPE_MISMATCH = "type_mismatch"
    """Field type doesn't match expected type."""

    # Semantic-level failures
    INVALID_REFERENCE = "invalid_reference"
    """Referenced entity doesn't exist or is inaccessible."""

    SEMANTIC_ERROR = "semantic_error"
    """Output is semantically invalid or inconsistent."""

    EVIDENCE_BINDING_ERROR = "evidence_binding_error"
    """Evidence is not properly bound to sources."""

    # Policy-level failures
    POLICY_VIOLATION = "policy_violation"
    """Operation violated policy constraints."""

    TOOL_DENIED = "tool_denied"
    """Attempted to use a denied tool."""

    FILE_ACCESS_DENIED = "file_access_denied"
    """Attempted to access a denied file."""

    SHELL_DENIED = "shell_denied"
    """Attempted to execute a denied shell command."""

    NETWORK_DENIED = "network_denied"
    """Attempted a denied network operation."""

    CAPABILITY_MISMATCH = "capability_mismatch"
    """Backend doesn't support required capability."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """Execution budget (tokens, turns, etc.) was exceeded."""

    CONTEXT_OVERFLOW = "context_overflow"
    """Context window exceeded."""


@dataclass(frozen=True)
class BackendFailure:
    """
    Normalized failure representation.

    All backend failures are converted to this common format for consistent
    handling by the orchestrator.
    """
    kind: BackendFailureKind
    """Classification of the failure."""

    message: str
    """Human-readable error message."""

    outcome_level: OutcomeLevel
    """Which outcome level this failure belongs to."""

    retryable: bool = False
    """Whether this failure can be retried."""

    backend_type: str | None = None
    """Which backend produced this failure (e.g., 'codex', 'claude_sdk')."""

    worker_role: str | None = None
    """Which worker role was being executed."""

    failure_stage: str | None = None
    """Stage within the adapter where failure occurred."""

    raw_error: str | None = None
    """Original error string from backend."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional context-specific metadata."""


# =============================================================================
# Capabilities
# =============================================================================

@dataclass(frozen=True)
class BackendCapabilities:
    """
    Declared capabilities of a backend.

    Capabilities are divided into:
    - declared: what the backend claims to support
    - runtime_allowed: subset allowed by policy envelope
    """
    # Execution capabilities
    supports_session_context: bool = False
    """Can maintain context across multiple invocations."""

    supports_agent_loop: bool = False
    """Can perform multi-turn reasoning with tool use."""

    supports_streaming: bool = False
    """Can stream output incrementally."""

    # Tool capabilities
    supports_file_read: bool = False
    """Can read files from filesystem."""

    supports_file_write: bool = False
    """Can write files to filesystem."""

    supports_shell: bool = False
    """Can execute shell commands."""

    supports_web: bool = False
    """Can make network/web requests."""

    # Output capabilities
    supports_structured_output_enforcement: bool = False
    """Can enforce structured output via schema."""

    supports_tool_restriction: bool = False
    """Can restrict which tools are available."""

    supports_model_override: bool = False
    """Can override the model being used."""

    # Metadata
    backend_type: str = "unknown"
    """Identifier for this backend type."""

    backend_version: str | None = None
    """Version of the backend."""

    def get_required_capabilities(self, worker_role: str) -> set[str]:
        """Get capabilities required for a given worker role."""
        # Default: most workers don't need special capabilities
        role_requirements = {
            "Reader": {"supports_file_read"},
            "Verifier": {"supports_file_read"},
            "IssueComposer": set(),  # Works with provided context only
            "CandidateGenerator": {"supports_file_read"},
        }
        return role_requirements.get(worker_role, set())


# =============================================================================
# Policy Envelope
# =============================================================================

@dataclass(frozen=True)
class BackendPolicyEnvelope:
    """
    Policy constraints for a backend invocation.

    The policy envelope is MANDATORY for all invocations.
    Default policy is DENY ALL - only explicitly allowed operations are permitted.

    HARD GUARD: Backends CANNOT expand their own policy envelope.
    The orchestrator is the only entity that can select/modify policies.
    """
    # Working directory constraints
    allowed_working_directory: str | None = None
    """Single allowed working directory."""

    allowed_roots: tuple[str, ...] = ()
    """Additional allowed root directories."""

    # File access policy
    allow_file_read: bool = False
    """Allow reading files."""

    allow_file_write: bool = False
    """Allow writing files."""

    allowed_file_patterns: tuple[str, ...] = ()
    """Glob patterns for allowed file access (e.g., '*.py', 'src/**')."""

    denied_file_patterns: tuple[str, ...] = ()
    """Glob patterns for explicitly denied file access."""

    # Shell policy
    allow_shell: bool = False
    """Allow shell command execution."""

    allowed_shell_commands: tuple[str, ...] = ()
    """Whitelist of allowed shell commands (e.g., 'git', 'npm')."""

    denied_shell_patterns: tuple[str, ...] = ()
    """Patterns for denied shell commands."""

    # Network policy
    allow_web: bool = False
    """Allow web/network requests."""

    allowed_domains: tuple[str, ...] = ()
    """Whitelist of allowed domains for web access."""

    # Budget constraints
    max_tool_calls: int | None = None
    """Maximum number of tool calls allowed."""

    max_wall_clock_seconds: int | None = None
    """Maximum wall-clock time in seconds."""

    max_tokens: int | None = None
    """Maximum tokens (if backend supports)."""

    max_agent_turns: int | None = None
    """Maximum agent loop iterations."""

    # Approval requirements
    require_approval_for_writes: bool = True
    """Require approval before any write operation."""

    require_approval_for_shell: bool = True
    """Require approval before any shell command."""

    # Environment
    allowed_env_vars: tuple[str, ...] = ()
    """Whitelist of allowed environment variables."""

    # Profile identification
    policy_profile_name: str = "default"
    """Name of the policy profile being applied."""

    @classmethod
    def deny_all(cls) -> "BackendPolicyEnvelope":
        """Create a policy that denies everything."""
        return cls(
            allow_file_read=False,
            allow_file_write=False,
            allow_shell=False,
            allow_web=False,
            max_tool_calls=0,
            max_wall_clock_seconds=60,
            policy_profile_name="deny_all",
        )

    @classmethod
    def read_only(cls, working_directory: str) -> "BackendPolicyEnvelope":
        """Create a read-only policy for a working directory."""
        return cls(
            allowed_working_directory=working_directory,
            allow_file_read=True,
            allow_file_write=False,
            allow_shell=False,
            allow_web=False,
            policy_profile_name="read_only",
        )


# =============================================================================
# Telemetry
# =============================================================================

@dataclass
class BackendTelemetry:
    """
    Telemetry data from a backend invocation.

    Always populated regardless of success/failure.
    """
    backend_type: str
    """Which backend was used."""

    model: str | None = None
    """Model identifier, if applicable."""

    invocation_id: str | None = None
    """Unique identifier for this invocation."""

    duration_seconds: float | None = None
    """Wall-clock duration of invocation."""

    tool_calls_count: int = 0
    """Number of tool calls made."""

    tokens_used: int | None = None
    """Total tokens used, if available."""

    tokens_input: int | None = None
    """Input tokens, if available."""

    tokens_output: int | None = None
    """Output tokens, if available."""

    policy_profile: str | None = None
    """Policy profile that was applied."""

    outcome_level_reached: OutcomeLevel = OutcomeLevel.PROCESS
    """Highest outcome level reached before completion or failure."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Backend-specific metadata."""


# =============================================================================
# Request and Result Types
# =============================================================================

@dataclass(frozen=True)
class BackendInvocationRequest:
    """
    Normalized request for backend invocation.
    """
    worker_role: str
    """Worker role to execute."""

    worker_input: dict[str, Any]
    """Worker-specific input data."""

    policy_envelope: BackendPolicyEnvelope
    """Policy constraints for this invocation."""

    # Optional overrides
    model_override: str | None = None
    """Override the default model."""

    invocation_id: str | None = None
    """Unique identifier for tracking."""


@dataclass
class BackendInvocationResult:
    """
    Normalized result from a backend invocation.
    """
    success: bool
    """Whether the invocation succeeded."""

    payload: dict[str, Any] | None = None
    """Structured output payload if success."""

    candidate_events: list[dict[str, Any]] = field(default_factory=list)
    """Candidate events extracted from payload."""

    error: BackendFailure | None = None
    """Failure details if not success."""

    telemetry: BackendTelemetry | None = None
    """Telemetry data (always populated in production)."""

    # Digests for reproducibility
    input_digest: str | None = None
    """Hash of input for reproducibility."""

    output_digest: str | None = None
    """Hash of output for reproducibility."""


# =============================================================================
# Backend Adapter Protocol
# =============================================================================

@runtime_checkable
class BackendAdapter(Protocol):
    """
    Protocol for all worker execution backends.

    All backends must implement this interface. The orchestrator uses this
    interface to invoke workers regardless of the underlying backend.

    Key contract:
    - run_with_result() returns BackendInvocationResult
    - All failures are normalized to BackendFailure
    - Backend has NO authority over state mutations
    """

    def get_capabilities(self) -> BackendCapabilities:
        """
        Return declared capabilities of this backend.

        Returns capabilities the backend supports, NOT what is currently allowed.
        Runtime-allowed capabilities are determined by policy envelope.
        """
        ...

    def run_with_result(
        self,
        worker_role: str,
        worker_input: dict[str, Any],
        policy_envelope: BackendPolicyEnvelope,
    ) -> BackendInvocationResult:
        """
        Execute one worker invocation and return normalized result.

        Args:
            worker_role: Role of worker to execute (e.g., "Reader", "Verifier")
            worker_input: Worker-specific input data
            policy_envelope: Policy constraints for this invocation

        Returns:
            BackendInvocationResult with success/failure and telemetry

        The backend MUST:
        - Respect all policy constraints
        - Normalize output to common format
        - Populate telemetry data
        - Classify failures appropriately

        The backend MUST NOT:
        - Write to event store
        - Mutate runtime state
        - Bypass policy constraints
        - Make routing decisions
        """
        ...

    def check_capability_compatibility(
        self,
        required_capabilities: set[str],
    ) -> tuple[bool, list[str]]:
        """
        Check if backend supports required capabilities.

        Args:
            required_capabilities: Set of capability names required

        Returns:
            Tuple of (is_compatible, missing_capabilities)
        """
        ...


# =============================================================================
# Helper Functions
# =============================================================================

def classify_exception(
    exc: Exception,
    backend_type: str,
    worker_role: str | None = None,
) -> BackendFailure:
    """
    Classify a generic exception into a BackendFailure.

    This is a fallback for exceptions that aren't already BackendFailure.
    """
    # Map common exception types to failure kinds
    exc_name = type(exc).__name__

    if exc_name in ("TimeoutError", "TimeoutExpired"):
        return BackendFailure(
            kind=BackendFailureKind.TIMEOUT,
            message=str(exc),
            outcome_level=OutcomeLevel.PROCESS,
            retryable=True,
            backend_type=backend_type,
            worker_role=worker_role,
            raw_error=str(exc),
        )

    if exc_name in ("FileNotFoundError", "ExecutableNotFound"):
        return BackendFailure(
            kind=BackendFailureKind.BACKEND_UNAVAILABLE,
            message=str(exc),
            outcome_level=OutcomeLevel.PROCESS,
            retryable=False,
            backend_type=backend_type,
            worker_role=worker_role,
            raw_error=str(exc),
        )

    if exc_name in ("JSONDecodeError",):
        return BackendFailure(
            kind=BackendFailureKind.INVALID_JSON,
            message=str(exc),
            outcome_level=OutcomeLevel.TRANSPORT,
            retryable=False,
            backend_type=backend_type,
            worker_role=worker_role,
            raw_error=str(exc),
        )

    # Default: unknown process failure
    return BackendFailure(
        kind=BackendFailureKind.PROCESS_CRASH,
        message=f"Unexpected error: {exc}",
        outcome_level=OutcomeLevel.PROCESS,
        retryable=False,
        backend_type=backend_type,
        worker_role=worker_role,
        raw_error=str(exc),
    )
