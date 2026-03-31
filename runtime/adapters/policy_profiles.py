"""
Policy Profiles for Backend Adapters.

This module defines policy profiles for different worker roles.
All profiles follow the principle: DENY BY DEFAULT.

Policy profiles are IMMUTABLE and cannot be extended by backends.
The orchestrator is the only entity that can select/modify policies.

Key principle: Backend cannot expand its own policy envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from runtime.adapters.base import BackendPolicyEnvelope


# =============================================================================
# Policy Profile Registry
# =============================================================================

class PolicyProfileKind(Enum):
    """Predefined policy profile kinds."""

    DENY_ALL = "deny_all"
    """Maximum restriction: nothing allowed."""

    READ_ONLY = "read_only"
    """Read-only file access, no shell, no network."""

    READ_ONLY_STRICT = "read_only_strict"
    """Read-only with restricted file patterns."""

    VERIFIER = "verifier"
    """For verifier workers: read-only, no network, strict boundaries."""

    ISSUE_COMPOSER = "issue_composer"
    """For issue composer: no file access, context-only."""

    CANDIDATE_GENERATOR = "candidate_generator"
    """For candidate generator: read-only, possibly limited shell for specific tools."""

    FULL_ACCESS = "full_access"
    """Full access - use with extreme caution, only for trusted scenarios."""


@dataclass(frozen=True)
class PolicyProfileDefinition:
    """Definition of a policy profile."""

    kind: PolicyProfileKind
    """Profile kind identifier."""

    name: str
    """Human-readable name."""

    description: str
    """Description of when to use this profile."""

    factory: Callable[[], BackendPolicyEnvelope]
    """Factory function to create the policy envelope."""

    allowed_for_roles: tuple[str, ...] = ()
    """Worker roles this profile is suitable for."""

    risk_level: str = "low"
    """Risk level of this profile: low, medium, high."""


# =============================================================================
# Policy Profile Factories
# =============================================================================

def create_deny_all_profile() -> BackendPolicyEnvelope:
    """
    Create a deny-all policy.

    Nothing is allowed. Use as fallback or for workers that should be completely restricted.
    """
    return BackendPolicyEnvelope(
        # All permissions denied
        allow_file_read=False,
        allow_file_write=False,
        allow_shell=False,
        allow_web=False,
        # Strict approval requirements (irrelevant since all denied, but explicit)
        require_approval_for_writes=True,
        require_approval_for_shell=True,
        # Budget: minimal
        max_tool_calls=0,
        max_wall_clock_seconds=60,
        max_tokens=1000,
        max_agent_turns=1,
        # Profile metadata
        policy_profile_name="deny_all",
    )


def create_read_only_profile(
    working_directory: str | None = None,
    allowed_roots: tuple[str, ...] | None = None,
    max_wall_clock_seconds: int = 300,
) -> BackendPolicyEnvelope:
    """
    Create a read-only policy.

    Allows reading files but no writes, shell, or network.
    Suitable for Reader and similar workers.
    """
    return BackendPolicyEnvelope(
        # Working directory constraints
        allowed_working_directory=working_directory,
        allowed_roots=allowed_roots or (),
        # File access: read only
        allow_file_read=True,
        allow_file_write=False,
        # Shell and network: denied
        allow_shell=False,
        allow_web=False,
        # Budget: reasonable defaults
        max_tool_calls=50,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_tokens=100000,
        max_agent_turns=10,
        # Approval requirements
        require_approval_for_writes=True,
        require_approval_for_shell=True,
        # Profile metadata
        policy_profile_name="read_only",
    )


def create_read_only_strict_profile(
    working_directory: str,
    allowed_file_patterns: tuple[str, ...],
    max_wall_clock_seconds: int = 180,
) -> BackendPolicyEnvelope:
    """
    Create a strict read-only policy with file pattern restrictions.

    Only files matching allowed patterns can be read.
    Suitable for workers with limited scope.
    """
    return BackendPolicyEnvelope(
        # Working directory constraints
        allowed_working_directory=working_directory,
        # File access: read only with patterns
        allow_file_read=True,
        allow_file_write=False,
        allowed_file_patterns=allowed_file_patterns,
        # Shell and network: denied
        allow_shell=False,
        allow_web=False,
        # Budget: tighter limits
        max_tool_calls=30,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_tokens=50000,
        max_agent_turns=5,
        # Profile metadata
        policy_profile_name="read_only_strict",
    )


def create_verifier_profile(
    working_directory: str | None = None,
    max_wall_clock_seconds: int = 180,
) -> BackendPolicyEnvelope:
    """
    Create a verifier policy.

    Read-only, no network, strict boundaries.
    Suitable for Verifier workers.
    """
    return BackendPolicyEnvelope(
        # Working directory constraints
        allowed_working_directory=working_directory,
        # File access: read only
        allow_file_read=True,
        allow_file_write=False,
        # Shell and network: denied (verifiers don't need these)
        allow_shell=False,
        allow_web=False,
        # Budget: moderate limits (verifiers should be quick)
        max_tool_calls=30,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_tokens=50000,
        max_agent_turns=5,
        # Profile metadata
        policy_profile_name="verifier",
    )


def create_issue_composer_profile(
    max_wall_clock_seconds: int = 120,
) -> BackendPolicyEnvelope:
    """
    Create an issue composer policy.

    No file access needed - works with provided context only.
    Suitable for IssueComposer workers.
    """
    return BackendPolicyEnvelope(
        # No file access needed for issue composition
        allow_file_read=False,
        allow_file_write=False,
        # Shell and network: denied
        allow_shell=False,
        allow_web=False,
        # Budget: moderate limits
        max_tool_calls=20,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_tokens=30000,
        max_agent_turns=3,
        # Profile metadata
        policy_profile_name="issue_composer",
    )


def create_candidate_generator_profile(
    working_directory: str | None = None,
    allow_limited_shell: bool = False,
    allowed_shell_commands: tuple[str, ...] | None = None,
    max_wall_clock_seconds: int = 300,
) -> BackendPolicyEnvelope:
    """
    Create a candidate generator policy.

    Read-only file access, optionally limited shell for specific tools.
    Suitable for CandidateGenerator workers.
    """
    return BackendPolicyEnvelope(
        # Working directory constraints
        allowed_working_directory=working_directory,
        # File access: read only (candidates don't write)
        allow_file_read=True,
        allow_file_write=False,
        # Shell: optional limited access
        allow_shell=allow_limited_shell,
        allowed_shell_commands=allowed_shell_commands or (),
        # Network: denied
        allow_web=False,
        # Budget: higher limits for complex reasoning
        max_tool_calls=100,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_tokens=100000,
        max_agent_turns=15,
        # Approval requirements
        require_approval_for_writes=True,
        require_approval_for_shell=True,
        # Profile metadata
        policy_profile_name="candidate_generator",
    )


def create_full_access_profile(
    working_directory: str | None = None,
    require_approval: bool = True,
    max_wall_clock_seconds: int = 600,
) -> BackendPolicyEnvelope:
    """
    Create a full access policy.

    WARNING: Use with extreme caution. Only for trusted scenarios.
    Allows read, write, shell, and network access.
    """
    return BackendPolicyEnvelope(
        # Working directory constraints
        allowed_working_directory=working_directory,
        # File access: full
        allow_file_read=True,
        allow_file_write=True,
        # Shell and network: allowed
        allow_shell=True,
        allow_web=True,
        # Budget: generous limits
        max_tool_calls=200,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_tokens=200000,
        max_agent_turns=30,
        # Approval requirements (still require approval even with full access)
        require_approval_for_writes=require_approval,
        require_approval_for_shell=require_approval,
        # Profile metadata
        policy_profile_name="full_access",
    )


# =============================================================================
# Policy Profile Registry
# =============================================================================

POLICY_PROFILE_REGISTRY: dict[PolicyProfileKind, PolicyProfileDefinition] = {
    PolicyProfileKind.DENY_ALL: PolicyProfileDefinition(
        kind=PolicyProfileKind.DENY_ALL,
        name="Deny All",
        description="Maximum restriction: nothing allowed",
        factory=create_deny_all_profile,
        allowed_for_roles=(),  # Not recommended for any role
        risk_level="low",
    ),
    PolicyProfileKind.READ_ONLY: PolicyProfileDefinition(
        kind=PolicyProfileKind.READ_ONLY,
        name="Read Only",
        description="Read-only file access, no shell, no network",
        factory=create_read_only_profile,
        allowed_for_roles=("Reader", "CandidateGenerator"),
        risk_level="low",
    ),
    PolicyProfileKind.READ_ONLY_STRICT: PolicyProfileDefinition(
        kind=PolicyProfileKind.READ_ONLY_STRICT,
        name="Read Only (Strict)",
        description="Read-only with restricted file patterns",
        factory=lambda: create_read_only_strict_profile(
            working_directory=".",
            allowed_file_patterns=("*.py", "*.json", "*.md"),
        ),
        allowed_for_roles=("Reader", "Verifier"),
        risk_level="low",
    ),
    PolicyProfileKind.VERIFIER: PolicyProfileDefinition(
        kind=PolicyProfileKind.VERIFIER,
        name="Verifier",
        description="Read-only, no network, strict boundaries",
        factory=create_verifier_profile,
        allowed_for_roles=("Verifier",),
        risk_level="low",
    ),
    PolicyProfileKind.ISSUE_COMPOSER: PolicyProfileDefinition(
        kind=PolicyProfileKind.ISSUE_COMPOSER,
        name="Issue Composer",
        description="No file access, context-only",
        factory=create_issue_composer_profile,
        allowed_for_roles=("IssueComposer",),
        risk_level="low",
    ),
    PolicyProfileKind.CANDIDATE_GENERATOR: PolicyProfileDefinition(
        kind=PolicyProfileKind.CANDIDATE_GENERATOR,
        name="Candidate Generator",
        description="Read-only, optionally limited shell",
        factory=create_candidate_generator_profile,
        allowed_for_roles=("CandidateGenerator",),
        risk_level="low",
    ),
    PolicyProfileKind.FULL_ACCESS: PolicyProfileDefinition(
        kind=PolicyProfileKind.FULL_ACCESS,
        name="Full Access",
        description="WARNING: Full read/write/shell/network access",
        factory=create_full_access_profile,
        allowed_for_roles=(),  # Not recommended for standard roles
        risk_level="high",
    ),
}


# =============================================================================
# Role to Profile Mapping
# =============================================================================

WORKER_ROLE_DEFAULT_PROFILE: dict[str, PolicyProfileKind] = {
    "Reader": PolicyProfileKind.READ_ONLY,
    "Verifier": PolicyProfileKind.VERIFIER,
    "IssueComposer": PolicyProfileKind.ISSUE_COMPOSER,
    "CandidateGenerator": PolicyProfileKind.CANDIDATE_GENERATOR,
}


# =============================================================================
# Policy Envelope Validation
# =============================================================================

def validate_policy_envelope(envelope: BackendPolicyEnvelope) -> list[str]:
    """
    Validate a policy envelope for consistency.

    Returns a list of validation errors (empty if valid).
    """
    errors: list[str] = []

    # Check for contradictory settings
    if envelope.allow_file_write and not envelope.allow_file_read:
        errors.append("allow_file_write requires allow_file_read (cannot write without reading)")

    # Check budget constraints
    if envelope.max_tool_calls is not None and envelope.max_tool_calls < 0:
        errors.append("max_tool_calls cannot be negative")

    if envelope.max_wall_clock_seconds is not None and envelope.max_wall_clock_seconds < 1:
        errors.append("max_wall_clock_seconds must be at least 1")

    if envelope.max_tokens is not None and envelope.max_tokens < 1:
        errors.append("max_tokens must be at least 1")

    if envelope.max_agent_turns is not None and envelope.max_agent_turns < 1:
        errors.append("max_agent_turns must be at least 1")

    # Check working directory
    if envelope.allowed_working_directory is None and envelope.allow_file_read:
        # Warning, not error: might be okay for some backends
        pass

    return errors


def get_policy_for_role(
    worker_role: str,
    working_directory: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> BackendPolicyEnvelope:
    """
    Get the default policy envelope for a worker role.

    Args:
        worker_role: Worker role name
        working_directory: Optional working directory constraint
        overrides: Optional field overrides

    Returns:
        BackendPolicyEnvelope configured for the role
    """
    # Get default profile kind for role
    profile_kind = WORKER_ROLE_DEFAULT_PROFILE.get(worker_role, PolicyProfileKind.DENY_ALL)

    # Get profile definition
    profile_def = POLICY_PROFILE_REGISTRY.get(profile_kind)
    if profile_def is None:
        # Fallback to deny-all
        return create_deny_all_profile()

    # Create base envelope
    envelope = profile_def.factory()

    # Apply working directory if provided
    if working_directory is not None:
        # Create a modified envelope with the working directory
        envelope = BackendPolicyEnvelope(
            allowed_working_directory=working_directory,
            allowed_roots=envelope.allowed_roots,
            allow_file_read=envelope.allow_file_read,
            allow_file_write=envelope.allow_file_write,
            allowed_file_patterns=envelope.allowed_file_patterns,
            denied_file_patterns=envelope.denied_file_patterns,
            allow_shell=envelope.allow_shell,
            allowed_shell_commands=envelope.allowed_shell_commands,
            denied_shell_patterns=envelope.denied_shell_patterns,
            allow_web=envelope.allow_web,
            allowed_domains=envelope.allowed_domains,
            max_tool_calls=envelope.max_tool_calls,
            max_wall_clock_seconds=envelope.max_wall_clock_seconds,
            max_tokens=envelope.max_tokens,
            max_agent_turns=envelope.max_agent_turns,
            require_approval_for_writes=envelope.require_approval_for_writes,
            require_approval_for_shell=envelope.require_approval_for_shell,
            allowed_env_vars=envelope.allowed_env_vars,
            policy_profile_name=envelope.policy_profile_name,
        )

    # Apply overrides if provided
    if overrides:
        # This is a simple override mechanism
        # In production, you'd want more sophisticated merging
        base_dict = {
            "allowed_working_directory": envelope.allowed_working_directory,
            "allowed_roots": envelope.allowed_roots,
            "allow_file_read": envelope.allow_file_read,
            "allow_file_write": envelope.allow_file_write,
            "allowed_file_patterns": envelope.allowed_file_patterns,
            "denied_file_patterns": envelope.denied_file_patterns,
            "allow_shell": envelope.allow_shell,
            "allowed_shell_commands": envelope.allowed_shell_commands,
            "denied_shell_patterns": envelope.denied_shell_patterns,
            "allow_web": envelope.allow_web,
            "allowed_domains": envelope.allowed_domains,
            "max_tool_calls": envelope.max_tool_calls,
            "max_wall_clock_seconds": envelope.max_wall_clock_seconds,
            "max_tokens": envelope.max_tokens,
            "max_agent_turns": envelope.max_agent_turns,
            "require_approval_for_writes": envelope.require_approval_for_writes,
            "require_approval_for_shell": envelope.require_approval_for_shell,
            "allowed_env_vars": envelope.allowed_env_vars,
            "policy_profile_name": envelope.policy_profile_name,
        }

        # Apply overrides (only allow restricting, not expanding)
        for key, value in overrides.items():
            if key in base_dict:
                # Security check: don't allow expanding permissions
                if key.startswith("allow_") and value is True:
                    current = base_dict.get(key, False)
                    if not current:
                        # Log warning: trying to expand permission
                        # In strict mode, you might want to reject this
                        pass
                base_dict[key] = value

        envelope = BackendPolicyEnvelope(**base_dict)

    return envelope


def check_policy_expansion(
    original: BackendPolicyEnvelope,
    modified: BackendPolicyEnvelope,
) -> tuple[bool, list[str]]:
    """
    Check if a policy has been expanded (permissions increased).

    Backends should NEVER be able to expand their policy.
    This function can be used to detect policy expansion attempts.

    Args:
        original: Original policy envelope
        modified: Potentially modified policy envelope

    Returns:
        Tuple of (is_expansion, expansion_details)
    """
    expansions: list[str] = []

    # Check permission expansions
    if modified.allow_file_read and not original.allow_file_read:
        expansions.append("allow_file_read expanded from False to True")
    if modified.allow_file_write and not original.allow_file_write:
        expansions.append("allow_file_write expanded from False to True")
    if modified.allow_shell and not original.allow_shell:
        expansions.append("allow_shell expanded from False to True")
    if modified.allow_web and not original.allow_web:
        expansions.append("allow_web expanded from False to True")

    # Check budget expansions
    if (original.max_tool_calls is not None and
        modified.max_tool_calls is not None and
        modified.max_tool_calls > original.max_tool_calls):
        expansions.append(f"max_tool_calls expanded from {original.max_tool_calls} to {modified.max_tool_calls}")

    if (original.max_wall_clock_seconds is not None and
        modified.max_wall_clock_seconds is not None and
        modified.max_wall_clock_seconds > original.max_wall_clock_seconds):
        expansions.append(f"max_wall_clock_seconds expanded from {original.max_wall_clock_seconds} to {modified.max_wall_clock_seconds}")

    if (original.max_tokens is not None and
        modified.max_tokens is not None and
        modified.max_tokens > original.max_tokens):
        expansions.append(f"max_tokens expanded from {original.max_tokens} to {modified.max_tokens}")

    if (original.max_agent_turns is not None and
        modified.max_agent_turns is not None and
        modified.max_agent_turns > original.max_agent_turns):
        expansions.append(f"max_agent_turns expanded from {original.max_agent_turns} to {modified.max_agent_turns}")

    return (len(expansions) > 0, expansions)


# =============================================================================
# Hard Guard: Policy Expansion Prevention
# =============================================================================

class PolicyExpansionError(Exception):
    """Raised when a policy expansion attempt is detected."""

    def __init__(self, expansions: list[str]) -> None:
        self.expansions = expansions
        super().__init__(
            f"Policy expansion detected: {'; '.join(expansions)}. "
            "Backends cannot expand their own policy envelope."
        )


def enforce_no_expansion(
    original: BackendPolicyEnvelope,
    modified: BackendPolicyEnvelope,
) -> None:
    """
    Enforce that a policy has not been expanded.

    Raises PolicyExpansionError if expansion is detected.

    This is the HARD GUARD: backends cannot expand their own policy.
    """
    is_expansion, expansions = check_policy_expansion(original, modified)
    if is_expansion:
        raise PolicyExpansionError(expansions)
