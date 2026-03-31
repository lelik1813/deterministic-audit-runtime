"""
Backend Adapters

This package provides adapter implementations for worker execution backends.

Available backends:
- CodexAdapter: Subprocess-based Codex CLI execution
- ClaudeAgentSdkAdapter: In-process Claude Agent SDK execution (skeleton)

Usage:
    from runtime.adapters import (
        BackendAdapter,
        BackendCapabilities,
        BackendPolicyEnvelope,
        BackendInvocationResult,
        BackendFailure,
        BackendFailureKind,
        OutcomeLevel,
        CapabilityKind,
        CapabilityMatrix,
        CapabilityNegotiator,
        PolicyProfileKind,
        get_policy_for_role,
        ClaudeAgentSdkAdapter,
        ClaudeSdkAdapterConfig,
    )

    from runtime.adapters.codex_adapter import CodexAdapter
"""

from runtime.adapters.base import (
    BackendAdapter,
    BackendCapabilities,
    BackendFailure,
    BackendFailureKind,
    BackendInvocationRequest,
    BackendInvocationResult,
    BackendPolicyEnvelope,
    BackendTelemetry,
    OutcomeLevel,
    classify_exception,
)

from runtime.adapters.capabilities import (
    CapabilityDefinition,
    CapabilityKind,
    CapabilityMatrix,
    CapabilityNegotiator,
    NegotiationResult,
    CAPABILITY_DEFINITIONS,
    WORKER_ROLE_CAPABILITY_REQUIREMENTS,
    get_claude_sdk_declared_capabilities,
    get_codex_declared_capabilities,
    policy_envelope_to_allowed_capabilities,
)

from runtime.adapters.policy_profiles import (
    PolicyExpansionError,
    PolicyProfileDefinition,
    PolicyProfileKind,
    WORKER_ROLE_DEFAULT_PROFILE,
    POLICY_PROFILE_REGISTRY,
    check_policy_expansion,
    create_candidate_generator_profile,
    create_deny_all_profile,
    create_full_access_profile,
    create_issue_composer_profile,
    create_read_only_profile,
    create_read_only_strict_profile,
    create_verifier_profile,
    enforce_no_expansion,
    get_policy_for_role,
    validate_policy_envelope,
)

from runtime.adapters.claude_agent_sdk_adapter import (
    ClaudeAgentSdkAdapter,
    ClaudeSdkAdapterConfig,
    ClaudeSdkClient,
    ClaudeSdkClientFactory,
    Clock,
    CollectedResult,
    InvocationSpec,
    NormalizedOutput,
    NullTelemetrySink,
    OutputNormalizer,
    RequestBuilder,
    SdkClientManager,
    StreamCollector,
    StreamEvent,
    SystemClock,
    TelemetrySink,
    TempStorage,
    ToolEvent,
    ToolEventObserver,
)

# Import CodexAdapter for type checking and error handling
from runtime.adapters.codex_adapter import CodexAdapter, CodexAdapterError

from runtime.adapters.selector import (
    AdapterFactoryConfig,
    BackendKind,
    BackendSelectionConfig,
    BackendSelector,
    BackendSelectionError,
    BackendUnavailableError,
    NoDefaultBackendError,
    create_adapter,
    select_and_create_adapter,
    get_default_selector,
    set_default_selector,
    reset_default_selector,
)

__all__ = [
    # ... existing ...
    # Protocol
    "BackendAdapter",
    # Capabilities (from base)
    "BackendCapabilities",
    # Policy
    "BackendPolicyEnvelope",
    # Results
    "BackendInvocationRequest",
    "BackendInvocationResult",
    "BackendTelemetry",
    # Failures
    "BackendFailure",
    "BackendFailureKind",
    "OutcomeLevel",
    # Helpers
    "classify_exception",
    # Capability Model
    "CapabilityKind",
    "CapabilityDefinition",
    "CapabilityMatrix",
    "CapabilityNegotiator",
    "NegotiationResult",
    "CAPABILITY_DEFINITIONS",
    "WORKER_ROLE_CAPABILITY_REQUIREMENTS",
    "get_claude_sdk_declared_capabilities",
    "get_codex_declared_capabilities",
    "policy_envelope_to_allowed_capabilities",
    # Policy Profiles
    "PolicyProfileKind",
    "PolicyProfileDefinition",
    "PolicyExpansionError",
    "POLICY_PROFILE_REGISTRY",
    "WORKER_ROLE_DEFAULT_PROFILE",
    "create_deny_all_profile",
    "create_read_only_profile",
    "create_read_only_strict_profile",
    "create_verifier_profile",
    "create_issue_composer_profile",
    "create_candidate_generator_profile",
    "create_full_access_profile",
    "get_policy_for_role",
    "validate_policy_envelope",
    "check_policy_expansion",
    "enforce_no_expansion",
    # Claude SDK Adapter
    "ClaudeAgentSdkAdapter",
    "ClaudeSdkAdapterConfig",
    "ClaudeSdkClient",
    "ClaudeSdkClientFactory",
    "Clock",
    "CollectedResult",
    "InvocationSpec",
    "NormalizedOutput",
    "NullTelemetrySink",
    "OutputNormalizer",
    "RequestBuilder",
    "SdkClientManager",
    "StreamCollector",
    "StreamEvent",
    "SystemClock",
    "TelemetrySink",
    "TempStorage",
    "ToolEvent",
    "ToolEventObserver",
    # Codex Adapter (for type checking and error handling)
    "CodexAdapter",
    "CodexAdapterError",
    # Backend Selection
    "BackendKind",
    "BackendSelectionConfig",
    "BackendSelector",
    "BackendSelectionError",
    "BackendUnavailableError",
    "NoDefaultBackendError",
    "AdapterFactoryConfig",
    "create_adapter",
    "select_and_create_adapter",
    "get_default_selector",
    "set_default_selector",
    "reset_default_selector",
]
