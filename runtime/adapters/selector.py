"""
Backend Selector - Minimal Implementation.

This module provides backend selection logic with explicit guarantees:
1. Default backend is explicitly configured (no implicit default)
2. Explicit backend selection is honored
3. NO implicit fallback to Codex when Claude fails
4. Clear error when requested backend is unavailable

The selector is deliberately minimal - it does NOT:
- Handle retry logic (orchestrator responsibility)
- Implement budget enforcement (policy layer)
- Manage backend lifecycle (adapter responsibility)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from runtime.adapters.base import BackendAdapter


class BackendKind(Enum):
    """Supported backend kinds."""

    CODEX = "codex"
    CLAUDE_SDK = "claude_sdk"


class BackendSelectionError(Exception):
    """Base error for backend selection failures."""

    def __init__(self, message: str, requested: BackendKind | None = None):
        super().__init__(message)
        self.requested = requested


class BackendUnavailableError(BackendSelectionError):
    """
    Raised when a backend is requested but unavailable.

    This error is raised (not silently falling back) when:
    - Claude SDK is requested but unavailable (not installed, no API key, etc.)
    - AND Codex is NOT explicitly requested as fallback

    IMPORTANT: The system does NOT implicitly fall back to Codex.
    Fallback must be explicitly requested by the orchestrator.
    """

    def __init__(
        self,
        requested: BackendKind,
        reason: str,
        available_backends: list[BackendKind] | None = None,
    ):
        self.reason = reason
        self.available_backends = available_backends or []
        message = (
            f"Backend '{requested.value}' is unavailable: {reason}. "
            f"Available backends: {[b.value for b in self.available_backends]}. "
            f"IMPLICIT FALLBACK TO CODEX IS NOT ALLOWED. "
            f"To use Codex, explicitly request --backend codex."
        )
        super().__init__(message, requested)


class NoDefaultBackendError(BackendSelectionError):
    """Raised when no default backend is configured."""

    def __init__(self):
        super().__init__(
            "No default backend configured. "
            "Either set a default backend or explicitly specify --backend."
        )


@dataclass(frozen=True)
class BackendSelectionConfig:
    """
    Configuration for backend selection.

    All fields are explicit - no implicit defaults that could hide behavior.
    """

    default_backend: BackendKind | None
    """Default backend to use when not explicitly specified. None = error if not specified."""

    claude_sdk_enabled: bool
    """Whether Claude SDK backend is available."""

    codex_enabled: bool
    """Whether Codex backend is available."""

    allow_explicit_fallback: bool
    """
    Whether orchestrator can explicitly request fallback.

    This does NOT enable automatic fallback - it only allows the
    orchestrator to explicitly select a different backend on error.
    """

    @classmethod
    def codex_only(cls) -> "BackendSelectionConfig":
        """Create config with only Codex available, Codex as default."""
        return cls(
            default_backend=BackendKind.CODEX,
            claude_sdk_enabled=False,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )

    @classmethod
    def claude_as_default(cls, codex_available: bool = True) -> "BackendSelectionConfig":
        """Create config with Claude SDK as default."""
        return cls(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=True,
            codex_enabled=codex_available,
            allow_explicit_fallback=True,
        )

    @classmethod
    def codex_as_default(cls, claude_available: bool = True) -> "BackendSelectionConfig":
        """Create config with Codex as default (original behavior)."""
        return cls(
            default_backend=BackendKind.CODEX,
            claude_sdk_enabled=claude_available,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )

    @property
    def available_backends(self) -> list[BackendKind]:
        """Get list of available backends."""
        backends = []
        if self.codex_enabled:
            backends.append(BackendKind.CODEX)
        if self.claude_sdk_enabled:
            backends.append(BackendKind.CLAUDE_SDK)
        return backends


class BackendSelector:
    """
    Selects backend based on configuration and explicit requests.

    Guarantees:
    1. Default backend is explicitly configured
    2. Explicit selection is always honored
    3. NO implicit fallback to Codex
    4. Clear error on unavailable backend
    """

    def __init__(self, config: BackendSelectionConfig):
        self._config = config

    @property
    def config(self) -> BackendSelectionConfig:
        """Get current configuration."""
        return self._config

    @property
    def default_backend(self) -> BackendKind:
        """
        Get the default backend.

        Raises NoDefaultBackendError if no default is configured.

        This is the ONLY place where default backend is determined.
        """
        if self._config.default_backend is None:
            raise NoDefaultBackendError()

        return self._config.default_backend

    def select(
        self,
        explicit_backend: BackendKind | None = None,
    ) -> BackendKind:
        """
        Select a backend.

        Args:
            explicit_backend: Explicitly requested backend, or None to use default.

        Returns:
            The selected backend kind.

        Raises:
            NoDefaultBackendError: No explicit request and no default configured.
            BackendUnavailableError: Requested backend is unavailable.

        IMPORTANT: This method NEVER implicitly falls back to Codex.
        If Claude is requested and unavailable, it raises BackendUnavailableError.
        The caller (orchestrator) must explicitly decide to use Codex.
        """
        # Determine which backend to use
        if explicit_backend is not None:
            backend = explicit_backend
        else:
            backend = self.default_backend  # May raise NoDefaultBackendError

        # Check availability
        self._check_availability(backend)

        return backend

    def _check_availability(self, backend: BackendKind) -> None:
        """
        Check if a backend is available.

        Raises BackendUnavailableError if not available.

        NOTE: This does NOT fall back to another backend.
        It raises an error that the orchestrator can handle.
        """
        if backend == BackendKind.CODEX:
            if not self._config.codex_enabled:
                raise BackendUnavailableError(
                    requested=backend,
                    reason="Codex backend is disabled",
                    available_backends=self._config.available_backends,
                )

        elif backend == BackendKind.CLAUDE_SDK:
            if not self._config.claude_sdk_enabled:
                raise BackendUnavailableError(
                    requested=backend,
                    reason="Claude SDK backend is disabled or not available",
                    available_backends=self._config.available_backends,
                )

        else:
            raise BackendUnavailableError(
                requested=backend,
                reason=f"Unknown backend kind: {backend}",
                available_backends=self._config.available_backends,
            )

    def is_available(self, backend: BackendKind) -> bool:
        """Check if a backend is available without raising."""
        try:
            self._check_availability(backend)
            return True
        except BackendUnavailableError:
            return False


# =============================================================================
# Module-level default selector (for CLI/config integration)
# =============================================================================

_DEFAULT_SELECTOR: BackendSelector | None = None


def get_default_selector() -> BackendSelector:
    """
    Get the default backend selector.

    Raises NoDefaultBackendError if no default selector is configured.
    """
    global _DEFAULT_SELECTOR
    if _DEFAULT_SELECTOR is None:
        raise NoDefaultBackendError()
    return _DEFAULT_SELECTOR


def set_default_selector(selector: BackendSelector) -> None:
    """Set the default backend selector."""
    global _DEFAULT_SELECTOR
    _DEFAULT_SELECTOR = selector


def reset_default_selector() -> None:
    """Reset the default selector (for testing)."""
    global _DEFAULT_SELECTOR
    _DEFAULT_SELECTOR = None


# =============================================================================
# Adapter Factory - THE ONLY PLACE WHERE ADAPTERS ARE CREATED
# =============================================================================

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.adapters.base import BackendAdapter
    from runtime.adapters.codex_adapter import CodexAdapter
    from runtime.adapters.claude_agent_sdk_adapter import ClaudeAgentSdkAdapter


@dataclass(frozen=True)
class AdapterFactoryConfig:
    """Configuration for creating adapter instances."""

    workspace_root: str | None = None
    invocation_dir: str | None = None
    model: str | None = None
    timeout_seconds: int = 300
    claude_sdk_config: Any = None  # ClaudeSdkAdapterConfig or dict


def create_adapter(
    backend: BackendKind,
    config: AdapterFactoryConfig,
) -> "BackendAdapter":
    """
    THE ONLY FUNCTION that creates adapter instances.

    This function is the SINGLE ENTRY POINT for all adapter creation.
    All code that needs an adapter MUST call this function.

    Args:
        backend: Which backend to create
        config: Configuration for the adapter

    Returns:
        A configured adapter instance

    Raises:
        BackendUnavailableError: If the backend is not available
        ValueError: If required config is missing for the backend

    IMPORTANT: This function does NOT select the backend.
    Use BackendSelector.select() first, then pass the result here.
    """
    if backend == BackendKind.CODEX:
        from runtime.adapters.codex_adapter import CodexAdapter

        return CodexAdapter(
            config.workspace_root or ".",
            invocation_dir=config.invocation_dir,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )

    elif backend == BackendKind.CLAUDE_SDK:
        from runtime.adapters.claude_agent_sdk_adapter import ClaudeAgentSdkAdapter

        if config.claude_sdk_config is None:
            raise ValueError(
                "claude_sdk_config is required for Claude SDK backend. "
                "Provide ClaudeSdkAdapterConfig via config.claude_sdk_config."
            )

        # Pass workspace_root directly to adapter (config is immutable)
        return ClaudeAgentSdkAdapter(
            config.claude_sdk_config,
            workspace_root=Path(config.workspace_root) if config.workspace_root else None,
        )

    else:
        raise BackendUnavailableError(
            requested=backend,
            reason=f"Unknown backend kind: {backend}",
            available_backends=[BackendKind.CODEX, BackendKind.CLAUDE_SDK],
        )


def select_and_create_adapter(
    selector: BackendSelector | None = None,
    explicit_backend: BackendKind | None = None,
    config: AdapterFactoryConfig | None = None,
) -> tuple[BackendKind, "BackendAdapter"]:
    """
    Convenience function that selects AND creates an adapter.

    This combines:
    1. BackendSelector.select() - determines which backend to use
    2. create_adapter() - creates the adapter instance

    Args:
        selector: Selector to use (default: module-level selector)
        explicit_backend: Explicitly request a backend (None = use default)
        config: Configuration for adapter creation

    Returns:
        Tuple of (selected backend kind, adapter instance)

    Raises:
        NoDefaultBackendError: No selector, no explicit backend, and no default
        BackendUnavailableError: Selected backend unavailable
    """
    # If explicit backend provided, use it directly (no selector needed)
    if explicit_backend is not None:
        backend = explicit_backend
        # Still check availability if selector provided
        if selector is not None:
            selector._check_availability(backend)
    else:
        # No explicit backend - need selector with default
        if selector is None:
            selector = get_default_selector()
        backend = selector.select(explicit_backend=None)

    # Create adapter
    if config is None:
        config = AdapterFactoryConfig()

    adapter = create_adapter(backend, config)

    return (backend, adapter)