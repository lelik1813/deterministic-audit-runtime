"""
Tests for Backend Selection Guarantees.

These tests verify the 4 critical backend selection behaviors:
1. Default backend selection works correctly
2. Explicit Codex selection works
3. NO implicit fallback to Codex when Claude fails
4. Clear error when Claude is unavailable and Codex is not explicitly requested

These tests are deliberately minimal - they test ONLY the selection logic,
not the full orchestration or adapter implementation.
"""

from __future__ import annotations

import pytest

from runtime.adapters.selector import (
    BackendKind,
    BackendSelectionConfig,
    BackendSelector,
    BackendUnavailableError,
    NoDefaultBackendError,
)


class TestDefaultBackendSelection:
    """
    Test 1: Default backend selection.

    Verifies that:
    - Default backend is explicitly configured (not implicit)
    - When no explicit backend is requested, default is used
    - Error is raised if no default is configured
    """

    def test_default_backend_codex(self):
        """When Codex is configured as default, it is selected."""
        config = BackendSelectionConfig.codex_as_default()
        selector = BackendSelector(config)

        result = selector.select(explicit_backend=None)

        assert result == BackendKind.CODEX

    def test_default_backend_claude(self):
        """When Claude SDK is configured as default, it is selected."""
        config = BackendSelectionConfig.claude_as_default()
        selector = BackendSelector(config)

        result = selector.select(explicit_backend=None)

        assert result == BackendKind.CLAUDE_SDK

    def test_no_default_configured_raises_error(self):
        """When no default is configured, selection raises error."""
        config = BackendSelectionConfig(
            default_backend=None,
            claude_sdk_enabled=True,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        with pytest.raises(NoDefaultBackendError) as exc_info:
            selector.select(explicit_backend=None)

        assert "No default backend configured" in str(exc_info.value)

    def test_default_backend_property_raises_when_none(self):
        """Accessing default_backend raises when not configured."""
        config = BackendSelectionConfig(
            default_backend=None,
            claude_sdk_enabled=True,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        with pytest.raises(NoDefaultBackendError):
            _ = selector.default_backend


class TestExplicitCodexSelection:
    """
    Test 2: Explicit Codex selection.

    Verifies that:
    - Explicit --backend codex always selects Codex
    - Explicit selection overrides default
    - Explicit Codex works even when Claude is default
    """

    def test_explicit_codex_overrides_claude_default(self):
        """Explicit Codex selection overrides Claude default."""
        config = BackendSelectionConfig.claude_as_default()
        selector = BackendSelector(config)

        result = selector.select(explicit_backend=BackendKind.CODEX)

        assert result == BackendKind.CODEX

    def test_explicit_codex_when_codex_is_default(self):
        """Explicit Codex selection works when Codex is default."""
        config = BackendSelectionConfig.codex_as_default()
        selector = BackendSelector(config)

        result = selector.select(explicit_backend=BackendKind.CODEX)

        assert result == BackendKind.CODEX

    def test_explicit_codex_when_codex_only_config(self):
        """Explicit Codex works in Codex-only configuration."""
        config = BackendSelectionConfig.codex_only()
        selector = BackendSelector(config)

        result = selector.select(explicit_backend=BackendKind.CODEX)

        assert result == BackendKind.CODEX


class TestNoImplicitFallbackToCodex:
    """
    Test 3: NO implicit fallback to Codex.

    This is the CRITICAL test class. It verifies that the system
    NEVER implicitly falls back to Codex when Claude is unavailable.

    The guarantee is enforced by:
    - BackendUnavailableError being raised (not silently switching)
    - Error message explicitly mentioning no implicit fallback
    - Orchestrator must explicitly request Codex fallback
    """

    def test_claude_unavailable_raises_not_fallback(self):
        """
        CRITICAL: Claude unavailable raises error, does NOT fall back to Codex.

        This test verifies the core guarantee:
        - Claude SDK is requested (as default)
        - Claude SDK is unavailable
        - Codex IS available
        - BUT: System raises error, does NOT select Codex
        """
        # Config: Claude is default, but Claude SDK is disabled
        # Codex is available
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,  # Claude unavailable
            codex_enabled=True,        # Codex available
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        # When selecting without explicit backend
        # MUST raise error, NOT fall back to Codex
        with pytest.raises(BackendUnavailableError) as exc_info:
            selector.select(explicit_backend=None)

        # Verify error is about Claude, not Codex
        assert exc_info.value.requested == BackendKind.CLAUDE_SDK
        assert "Claude SDK" in str(exc_info.value) or "claude_sdk" in str(exc_info.value)

    def test_explicit_claude_unavailable_raises_not_fallback(self):
        """
        Explicit Claude request when unavailable raises error.

        Even when Codex is available, explicit Claude request
        that fails does NOT fall back to Codex.
        """
        config = BackendSelectionConfig(
            default_backend=BackendKind.CODEX,  # Codex is default
            claude_sdk_enabled=False,           # Claude unavailable
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        # Explicit Claude request
        with pytest.raises(BackendUnavailableError) as exc_info:
            selector.select(explicit_backend=BackendKind.CLAUDE_SDK)

        assert exc_info.value.requested == BackendKind.CLAUDE_SDK

    def test_both_unavailable_raises_clear_error(self):
        """When both backends unavailable, clear error is raised."""
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,
            codex_enabled=False,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        with pytest.raises(BackendUnavailableError) as exc_info:
            selector.select(explicit_backend=None)

        # Error should list available backends (empty)
        assert exc_info.value.available_backends == []

    def test_no_silent_switch_to_codex(self):
        """
        Verify that select() NEVER returns CODEX when Claude was requested.

        This is a meta-test: regardless of configuration, if Claude is
        requested, we never silently return CODEX.
        """
        # Multiple configurations to test
        configs = [
            # (config, should_work)
            (BackendSelectionConfig.claude_as_default(), True),
            (BackendSelectionConfig.codex_as_default(), True),
            (BackendSelectionConfig.codex_only(), False),
        ]

        for config, should_work in configs:
            selector = BackendSelector(config)

            if should_work:
                # Should succeed with Claude
                result = selector.select(explicit_backend=BackendKind.CLAUDE_SDK)
                assert result == BackendKind.CLAUDE_SDK, (
                    f"Expected CLAUDE_SDK, got {result} for config {config}"
                )
            else:
                # Should raise error, NOT return CODEX
                with pytest.raises(BackendUnavailableError) as exc_info:
                    selector.select(explicit_backend=BackendKind.CLAUDE_SDK)

                # Critical: the error should NOT have selected Codex
                # The requested backend should be Claude
                assert exc_info.value.requested == BackendKind.CLAUDE_SDK


class TestClearErrorOnClaudeUnavailable:
    """
    Test 4: Clear error when Claude unavailable and Codex not explicitly requested.

    Verifies that:
    - Error message clearly states Claude is unavailable
    - Error message mentions no implicit fallback
    - Error message shows available backends
    - Error message tells user how to proceed
    """

    def test_error_message_mentions_no_fallback(self):
        """Error message explicitly mentions no implicit fallback."""
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        with pytest.raises(BackendUnavailableError) as exc_info:
            selector.select(explicit_backend=None)

        error_msg = str(exc_info.value)

        # Error must mention no implicit fallback
        assert "implicit fallback" in error_msg.lower() or "NOT allowed" in error_msg

    def test_error_message_shows_available_backends(self):
        """Error message lists available backends."""
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        with pytest.raises(BackendUnavailableError) as exc_info:
            selector.select(explicit_backend=None)

        # Should list Codex as available
        assert BackendKind.CODEX in exc_info.value.available_backends

    def test_error_message_tells_how_to_proceed(self):
        """Error message tells user to explicitly request Codex."""
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        with pytest.raises(BackendUnavailableError) as exc_info:
            selector.select(explicit_backend=None)

        error_msg = str(exc_info.value)

        # Should mention how to proceed
        assert "codex" in error_msg.lower() or "--backend" in error_msg

    def test_error_includes_reason(self):
        """Error includes reason for unavailability."""
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        with pytest.raises(BackendUnavailableError) as exc_info:
            selector.select(explicit_backend=None)

        # Should have a reason
        assert exc_info.value.reason is not None
        assert len(exc_info.value.reason) > 0


class TestExplicitFallbackWorks:
    """
    Verify that explicit fallback (not implicit) works correctly.

    This demonstrates that the orchestrator CAN explicitly select
    Codex when Claude is unavailable - but it must be EXPLICIT.
    """

    def test_explicit_codex_when_claude_unavailable(self):
        """
        Orchestrator can explicitly select Codex when Claude is unavailable.

        This is the correct way to handle Claude unavailability:
        1. Catch BackendUnavailableError
        2. Decide to use Codex
        3. Explicitly select Codex
        """
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,  # Claude unavailable
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        # Step 1: Try Claude (raises error)
        with pytest.raises(BackendUnavailableError):
            selector.select(explicit_backend=BackendKind.CLAUDE_SDK)

        # Step 2: Orchestrator decides to use Codex
        # Step 3: Explicitly select Codex
        result = selector.select(explicit_backend=BackendKind.CODEX)
        assert result == BackendKind.CODEX

    def test_is_available_helper(self):
        """is_available() can check backend availability without raising."""
        config = BackendSelectionConfig(
            default_backend=BackendKind.CLAUDE_SDK,
            claude_sdk_enabled=False,
            codex_enabled=True,
            allow_explicit_fallback=True,
        )
        selector = BackendSelector(config)

        assert selector.is_available(BackendKind.CODEX) is True
        assert selector.is_available(BackendKind.CLAUDE_SDK) is False


class TestConfigFactoryMethods:
    """Test configuration factory methods."""

    def test_codex_only_config(self):
        """codex_only() creates correct config."""
        config = BackendSelectionConfig.codex_only()

        assert config.default_backend == BackendKind.CODEX
        assert config.codex_enabled is True
        assert config.claude_sdk_enabled is False

    def test_claude_as_default_config(self):
        """claude_as_default() creates correct config."""
        config = BackendSelectionConfig.claude_as_default()

        assert config.default_backend == BackendKind.CLAUDE_SDK
        assert config.claude_sdk_enabled is True
        assert config.codex_enabled is True

    def test_codex_as_default_config(self):
        """codex_as_default() creates correct config."""
        config = BackendSelectionConfig.codex_as_default()

        assert config.default_backend == BackendKind.CODEX
        assert config.codex_enabled is True
        assert config.claude_sdk_enabled is True


class TestModuleLevelSelector:
    """Test module-level default selector functions."""

    def test_get_default_selector_raises_when_not_set(self):
        """get_default_selector raises when not configured."""
        from runtime.adapters.selector import (
            get_default_selector,
            reset_default_selector,
        )

        reset_default_selector()

        with pytest.raises(NoDefaultBackendError):
            get_default_selector()

    def test_set_and_get_default_selector(self):
        """set_default_selector configures module-level default."""
        from runtime.adapters.selector import (
            get_default_selector,
            reset_default_selector,
            set_default_selector,
        )

        reset_default_selector()

        config = BackendSelectionConfig.codex_as_default()
        selector = BackendSelector(config)
        set_default_selector(selector)

        assert get_default_selector() is selector

        reset_default_selector()  # Cleanup
