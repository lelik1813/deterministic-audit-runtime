"""
Claude Agent SDK Adapter Skeleton.

This module provides the adapter skeleton for the Claude Agent SDK backend.
It implements the BackendAdapter protocol defined in runtime.adapters.base.

IMPORTANT: This is a SKELETON with full interface surface but NO production logic.
The actual SDK integration will be added in later phases.

Architecture:
- All invocations go through run_with_backend_contract()
- Policy envelope is MANDATORY
- Backend has NO authority to expand its own policy
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from runtime.adapters.base import (
    BackendCapabilities,
    BackendFailure,
    BackendFailureKind,
    BackendInvocationResult,
    BackendPolicyEnvelope,
    BackendTelemetry,
    OutcomeLevel,
    classify_exception,
)
from runtime.adapters.capabilities import (
    CapabilityKind,
    CapabilityMatrix,
    CapabilityNegotiator,
    get_claude_sdk_declared_capabilities,
    policy_envelope_to_allowed_capabilities,
)
from runtime.adapters.policy_profiles import (
    enforce_no_expansion,
    validate_policy_envelope,
)


# =============================================================================
# Adapter Configuration
# =============================================================================

@dataclass(frozen=True)
class ClaudeSdkAdapterConfig:
    """
    Configuration for Claude Agent SDK adapter.

    All settings are immutable after creation.
    """

    # API Configuration
    api_key_source: str = "env://ANTHROPIC_API_KEY"
    """Source for API key: 'env://VAR_NAME' or 'file://path'."""

    model_name: str = "claude-sonnet-4-20250514"
    """Default model to use."""

    # Timeout Configuration
    default_timeout_seconds: int = 300
    """Default wall-clock timeout for invocations."""

    max_agent_turns: int = 20
    """Maximum agent loop iterations."""

    max_tool_calls: int = 100
    """Maximum tool calls per invocation."""

    max_output_tokens: int = 4096
    """Maximum output tokens for model response."""

    # Working Directory
    working_directory: str | None = None
    """Working directory for file operations."""

    # Sandbox Settings
    sandbox_enabled: bool = True
    """Enable sandbox mode for file/shell operations."""

    sandbox_write_allowed: bool = False
    """Allow writes in sandbox mode."""

    # Tool Policy
    allow_file_tools: bool = True
    """Enable file read/write tools."""

    allow_shell_tool: bool = False
    """Enable shell execution tool."""

    allow_web_tool: bool = False
    """Enable web access tool."""

    # Workspace
    workspace_root: str | None = None
    """Root directory of the workspace (for loading prompts etc)."""

    # Debug/Telemetry
    debug_mode: bool = False
    """Enable debug logging."""

    telemetry_enabled: bool = True
    """Enable telemetry collection."""

    @classmethod
    def default(cls) -> "ClaudeSdkAdapterConfig":
        """Create default configuration."""
        return cls()

    @classmethod
    def for_worker_role(cls, worker_role: str) -> "ClaudeSdkAdapterConfig":
        """Create configuration tailored for a worker role."""
        base = cls.default()

        # Adjust settings based on role
        if worker_role == "Reader":
            return cls(
                **{**base.__dict__, "allow_file_tools": True, "allow_shell_tool": False}
            )
        elif worker_role == "Verifier":
            return cls(
                **{**base.__dict__, "allow_file_tools": True, "allow_shell_tool": False}
            )
        elif worker_role == "IssueComposer":
            return cls(
                **{**base.__dict__, "allow_file_tools": False, "allow_shell_tool": False}
            )
        elif worker_role == "CandidateGenerator":
            return cls(
                **{**base.__dict__, "allow_file_tools": True, "allow_shell_tool": False}
            )
        return base


# =============================================================================
# SDK Client Protocol (for dependency injection)
# =============================================================================

class ClaudeSdkClient(Protocol):
    """Protocol for Claude SDK client (allows mocking in tests)."""

    def invoke(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Invoke the Claude model."""
        ...


class ClaudeSdkClientFactory(Protocol):
    """Protocol for SDK client factory (allows dependency injection)."""

    def create_client(self, config: ClaudeSdkAdapterConfig) -> ClaudeSdkClient:
        """Create an SDK client from configuration."""
        ...


# =============================================================================
# Clock Protocol (for dependency injection)
# =============================================================================

class Clock(Protocol):
    """Protocol for clock (allows mocking in tests)."""

    def now(self) -> datetime:
        """Get current time."""
        ...

    def timestamp(self) -> float:
        """Get current timestamp."""
        ...


class SystemClock:
    """Default system clock implementation."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def timestamp(self) -> float:
        return time.time()


# =============================================================================
# Event Sink Protocol (for telemetry)
# =============================================================================

class TelemetrySink(Protocol):
    """Protocol for telemetry sink (allows mocking in tests)."""

    def emit(self, event: dict[str, Any]) -> None:
        """Emit a telemetry event."""
        ...


class NullTelemetrySink:
    """Null sink that discards all events."""

    def emit(self, event: dict[str, Any]) -> None:
        pass


# =============================================================================
# Temp Storage Protocol (for temporary files)
# =============================================================================

class TempStorage(Protocol):
    """Protocol for temporary storage (allows mocking in tests)."""

    def create_temp_file(self, suffix: str = ".json") -> str:
        """Create a temporary file and return its path."""
        ...

    def cleanup_temp_file(self, path: str) -> None:
        """Clean up a temporary file."""
        ...


# =============================================================================
# Request Builder Stage
# =============================================================================

@dataclass
class InvocationSpec:
    """Specification for a single Claude SDK invocation."""

    worker_role: str
    """Worker role being invoked."""

    worker_input: dict[str, Any]
    """Worker-specific input data."""

    task_prompt: str
    """Main task prompt."""

    system_prompt: str | None = None
    """System prompt for Claude."""

    constraints: list[str] = field(default_factory=list)
    """Additional constraints for the invocation."""

    schema_hint: dict[str, Any] | None = None
    """Schema hint for structured output."""


class RequestBuilder:
    """
    Stage 1: Build invocation specification from worker input.

    Responsibilities:
    - Extract task prompt from worker input
    - Build system prompt with constraints
    - Add schema hints for structured output
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._workspace_root = workspace_root

    def build_spec(
        self,
        worker_role: str,
        worker_input: dict[str, Any],
        policy_envelope: BackendPolicyEnvelope,
    ) -> InvocationSpec:
        """Build invocation specification from worker input."""
        # Extract task prompt from input
        task = worker_input.get("task", {})
        task_prompt = self._build_task_prompt(worker_role, task, worker_input)

        # Build system prompt
        system_prompt = self._build_system_prompt(worker_role, policy_envelope)

        # Build constraints
        constraints = self._build_constraints(policy_envelope)

        # Schema hint
        schema_hint = self._get_schema_hint(worker_role)

        return InvocationSpec(
            worker_role=worker_role,
            worker_input=worker_input,
            task_prompt=task_prompt,
            system_prompt=system_prompt,
            constraints=constraints,
            schema_hint=schema_hint,
        )

    def _build_task_prompt(
        self,
        worker_role: str,
        task: dict[str, Any],
        worker_input: dict[str, Any],
    ) -> str:
        """Build the main task prompt by loading the prompt template for the worker role."""
        import json

        # Map worker role to prompt file
        role_to_prompt = {
            "Reader": "reader.md",
            "Verifier": "verifier.md",
            "IssueComposer": "issue_composer.md",
            "CandidateGenerator": "candidate_generator.md",
        }

        prompt_filename = role_to_prompt.get(worker_role, f"{worker_role.lower()}.md")

        # Try to load the prompt template
        prompt_template = None
        if self._workspace_root:
            prompt_path = self._workspace_root / "prompts" / prompt_filename
            if prompt_path.exists():
                prompt_template = prompt_path.read_text(encoding="utf-8")

        # Fallback if template not found
        if not prompt_template:
            return f"Execute {worker_role} task: {task.get('type', 'unknown')}"

        # Render the prompt with worker input JSON appended
        serialized_input = json.dumps(worker_input, ensure_ascii=True, sort_keys=True, indent=2)
        return (
            prompt_template.rstrip()
            + "\n\nWorker Input JSON:\n"
            + "WORKER_INPUT_JSON_BEGIN\n"
            + serialized_input
            + "\nWORKER_INPUT_JSON_END\n"
        )

    def _build_system_prompt(
        self,
        worker_role: str,
        policy_envelope: BackendPolicyEnvelope,
    ) -> str:
        """Build system prompt with constraints."""
        # SKELETON: Real implementation will build full system prompt
        return f"You are a {worker_role} worker. Follow all policy constraints."

    def _build_constraints(self, policy_envelope: BackendPolicyEnvelope) -> list[str]:
        """Build list of constraints from policy envelope."""
        constraints = []

        if not policy_envelope.allow_file_write:
            constraints.append("You cannot write files.")

        if not policy_envelope.allow_shell:
            constraints.append("You cannot execute shell commands.")

        if not policy_envelope.allow_web:
            constraints.append("You cannot make network requests.")

        if policy_envelope.max_tool_calls:
            constraints.append(f"Maximum {policy_envelope.max_tool_calls} tool calls allowed.")

        return constraints

    def _get_schema_hint(self, worker_role: str) -> dict[str, Any] | None:
        """Get schema hint for structured output."""
        # SKELETON: Real implementation will return actual schemas
        return {"type": "object", "properties": {}}


# =============================================================================
# Prompt Assembler Stage
# =============================================================================

class PromptAssembler:
    """
    Stage 2: Assemble prompts for Claude SDK.

    Responsibilities:
    - Combine system prompt with task prompt
    - Add output format instructions
    - Add evidence binding instructions
    """

    def assemble(
        self,
        spec: InvocationSpec,
        policy_envelope: BackendPolicyEnvelope,
    ) -> str:
        """Assemble the complete prompt for Claude."""
        parts = []

        # Add task prompt
        parts.append(spec.task_prompt)

        # Add constraints
        if spec.constraints:
            parts.append("\n## Constraints\n")
            parts.extend(f"- {c}\n" for c in spec.constraints)

        # Add output format
        parts.append("\n## Output Format\n")
        parts.append(self._get_output_format_instructions(spec.worker_role))

        return "".join(parts)

    def _get_output_format_instructions(self, worker_role: str) -> str:
        """Get output format instructions including transport envelope for the worker role."""
        return (
            'Return a single JSON object with this exact structure:\n'
            '{"schema_version": "1.0.0", "slice_id": "<copy from input>", '
            '"worker_role": "<copy from input>", "task_id": "<copy from input.task.id>", '
            '"candidate_events": [<one event object per finding>]}\n\n'
            "Transport Candidate Event Rules:\n"
            "- Each entry in candidate_events must contain ONLY event_type and payload.\n"
            "- Do NOT include these internal fields; the adapter adds them: "
            "id, entity_id, entity_type, occurred_at, actor, snapshot_ref, "
            "idempotency_key, caused_by_event_id, acceptance, schema_version.\n\n"
            "Transport Payload Rules:\n"
            "- observation.proposed: put main text in payload.claim, "
            "source-bound evidence in payload.evidence (array of objects with "
            "file_path, line_start, line_end, snapshot_ref).\n"
            "- hypothesis.proposed: put main text in payload.claim, "
            "rationale in payload.rationale.\n"
            "- question.opened: put question in payload.question, "
            "optional context in payload.context.\n\n"
            "Transport Evidence Item Shape:\n"
            "- Each payload.evidence entry: file_path (string), line_start (int), "
            "line_end (int), snapshot_ref (string).\n\n"
            "CRITICAL: Every observation.proposed MUST include at least one "
            "evidence item with file_path and line numbers."
        )


# =============================================================================
# SDK Client Lifecycle Stage
# =============================================================================

class SdkClientManager:
    """
    Stage 3: Manage SDK client lifecycle.

    Responsibilities:
    - Create SDK client
    - Handle authentication
    - Manage client reuse/disposal
    """

    def __init__(
        self,
        config: ClaudeSdkAdapterConfig,
        client_factory: ClaudeSdkClientFactory | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._client: ClaudeSdkClient | None = None

    def get_client(self) -> ClaudeSdkClient:
        """Get or create SDK client."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _create_client(self) -> ClaudeSdkClient:
        """Create a new SDK client."""
        # SKELETON: Real implementation will create actual SDK client
        raise NotImplementedError(
            "Claude SDK client creation not implemented. "
            "Inject a mock client factory for testing."
        )

    def dispose(self) -> None:
        """Dispose of the SDK client."""
        self._client = None


# =============================================================================
# Stream Collector Stage
# =============================================================================

@dataclass
class StreamEvent:
    """Event from the SDK stream."""

    kind: str
    """Event kind: 'text', 'tool_use', 'tool_result', 'error', 'done'."""

    data: dict[str, Any] = field(default_factory=dict)
    """Event-specific data."""


@dataclass
class CollectedResult:
    """Result collected from stream."""

    text: str = ""
    """Accumulated text output."""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """Tool calls made."""

    tool_results: list[dict[str, Any]] = field(default_factory=list)
    """Tool results received."""

    errors: list[str] = field(default_factory=list)
    """Errors encountered."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""


class StreamCollector:
    """
    Stage 4: Collect results from SDK stream.

    Responsibilities:
    - Observe stream events
    - Accumulate text output
    - Track tool usage
    - Detect errors
    """

    def collect(self, stream: Any) -> CollectedResult:
        """Collect results from a stream."""
        result = CollectedResult()

        # SKELETON: Real implementation will iterate stream
        # for event in stream:
        #     self._process_event(event, result)

        return result

    def _process_event(self, event: StreamEvent, result: CollectedResult) -> None:
        """Process a single stream event."""
        if event.kind == "text":
            result.text += event.data.get("text", "")
        elif event.kind == "tool_use":
            result.tool_calls.append(event.data)
        elif event.kind == "tool_result":
            result.tool_results.append(event.data)
        elif event.kind == "error":
            result.errors.append(event.data.get("message", "Unknown error"))


# =============================================================================
# Tool Event Observer Stage
# =============================================================================

@dataclass
class ToolEvent:
    """A tool usage event."""

    tool_name: str
    """Name of the tool used."""

    tool_input: dict[str, Any]
    """Input to the tool."""

    tool_result: Any
    """Result from the tool."""

    allowed: bool = True
    """Whether the tool use was allowed by policy."""

    timestamp: str = ""
    """ISO timestamp of the event."""


class ToolEventObserver:
    """
    Stage 5: Observe and validate tool usage.

    Responsibilities:
    - Observe tool usage events
    - Validate against policy envelope
    - Track tool call count
    - Detect policy violations
    """

    def __init__(self, policy_envelope: BackendPolicyEnvelope) -> None:
        self.policy = policy_envelope
        self.tool_events: list[ToolEvent] = []
        self._violation_detected = False

    def observe(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
    ) -> bool:
        """
        Observe a tool usage event.

        Returns True if allowed, False if denied by policy.
        """
        allowed = self._check_tool_allowed(tool_name, tool_input)

        event = ToolEvent(
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            allowed=allowed,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        self.tool_events.append(event)

        if not allowed:
            self._violation_detected = True

        return allowed

    def _check_tool_allowed(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Check if a tool use is allowed by policy."""
        # File tools
        if tool_name in ("read_file", "read_multiple_files"):
            return self.policy.allow_file_read

        if tool_name in ("write_file", "edit_file"):
            return self.policy.allow_file_write

        # Shell tool
        if tool_name == "shell":
            return self.policy.allow_shell

        # Web tool
        if tool_name in ("web_fetch", "web_search"):
            return self.policy.allow_web

        # Default: allow unknown tools (they might be internal)
        return True

    def get_tool_call_count(self) -> int:
        """Get total tool call count."""
        return len(self.tool_events)

    def has_violation(self) -> bool:
        """Check if any policy violation occurred."""
        return self._violation_detected

    def get_violations(self) -> list[ToolEvent]:
        """Get all tool events that violated policy."""
        return [e for e in self.tool_events if not e.allowed]


# =============================================================================
# Output Normalizer Stage
# =============================================================================

@dataclass
class NormalizedOutput:
    """Normalized output from the backend."""

    payload: dict[str, Any]
    """Structured payload."""

    candidate_events: list[dict[str, Any]] = field(default_factory=list)
    """Candidate events extracted from payload."""

    raw_text: str = ""
    """Raw text output (for debugging)."""

    parse_warnings: list[str] = field(default_factory=list)
    """Warnings encountered during parsing."""


class OutputNormalizer:
    """
    Stage 6: Normalize output to common format.

    Responsibilities:
    - Parse JSON output
    - Extract candidate events
    - Handle malformed output
    - Add metadata
    """

    def normalize(
        self,
        raw_output: str,
        worker_role: str,
        worker_input: dict[str, Any],
    ) -> NormalizedOutput:
        """Normalize raw output to common format."""
        import json
        import re

        warnings: list[str] = []
        payload: dict[str, Any] = {}
        candidate_events: list[dict[str, Any]] = []

        # Strip markdown fences if present
        cleaned_output = raw_output.strip()
        if cleaned_output.startswith("```"):
            # Remove opening fence (with optional language specifier)
            cleaned_output = re.sub(r'^```[\w]*\n?', '', cleaned_output)
            # Remove closing fence
            cleaned_output = re.sub(r'\n?```$', '', cleaned_output)
            cleaned_output = cleaned_output.strip()
            warnings.append("Stripped markdown fences from output")

        # Try to parse as JSON
        try:
            parsed = json.loads(cleaned_output)

            if isinstance(parsed, dict):
                # Check for transport envelope
                if "payload_json" in parsed:
                    inner = json.loads(parsed["payload_json"])
                    payload = inner
                elif "payload" in parsed:
                    payload = parsed["payload"]
                else:
                    payload = parsed

                # Extract candidate events
                candidate_events = payload.get("candidate_events", [])

                # Normalize raw candidate events to transport format so they
                # can be processed by CodexAdapter._normalize_transport_candidate_event
                # downstream in _convert_to_codex_result.
                if candidate_events and isinstance(candidate_events, list):
                    from runtime.adapters.codex_adapter import CodexAdapter
                    occurred_at = CodexAdapter._utc_now()
                    normalized_events: list[dict[str, Any]] = []
                    for idx, ce in enumerate(candidate_events):
                        try:
                            wrapped = ClaudeAgentSdkAdapter._ensure_transport_format(ce)
                            norm = CodexAdapter._normalize_transport_candidate_event(
                                worker_role,
                                worker_input,
                                wrapped,
                                index=idx,
                                occurred_at=occurred_at,
                            )
                            normalized_events.append(norm)
                        except Exception:
                            normalized_events.append(json.loads(json.dumps(ce)))
                    candidate_events = normalized_events

            else:
                warnings.append(f"Expected dict, got {type(parsed).__name__}")

        except json.JSONDecodeError as e:
            warnings.append(f"JSON parse error: {e}")
            # Store raw text for debugging
            payload = {"raw_text": raw_output}

        return NormalizedOutput(
            payload=payload,
            candidate_events=candidate_events,
            raw_text=raw_output,
            parse_warnings=warnings,
        )


# =============================================================================
# Error Mapper Stage
# =============================================================================

class ErrorMapper:
    """
    Stage 7: Map errors to normalized BackendFailure.

    Responsibilities:
    - Classify errors by kind
    - Determine outcome level
    - Determine retryability
    """

    def map_error(
        self,
        error: Exception,
        worker_role: str,
        outcome_level: OutcomeLevel = OutcomeLevel.PROCESS,
    ) -> BackendFailure:
        """Map an exception to a BackendFailure."""
        # Use the common classifier first
        failure = classify_exception(error, "claude_sdk", worker_role)
        # Return new instance with updated outcome_level (frozen dataclass)
        return BackendFailure(
            kind=failure.kind,
            message=failure.message,
            outcome_level=outcome_level,
            retryable=failure.retryable,
            backend_type=failure.backend_type,
            worker_role=worker_role,
            failure_stage=failure.failure_stage,
            raw_error=failure.raw_error,
            metadata=failure.metadata,
        )

    def map_timeout(self, worker_role: str, timeout_seconds: int) -> BackendFailure:
        """Map a timeout to a BackendFailure."""
        return BackendFailure(
            kind=BackendFailureKind.TIMEOUT,
            message=f"Claude SDK invocation timed out after {timeout_seconds} seconds",
            outcome_level=OutcomeLevel.PROCESS,
            retryable=True,
            backend_type="claude_sdk",
            worker_role=worker_role,
        )

    def map_policy_violation(
        self,
        worker_role: str,
        violation_details: str,
    ) -> BackendFailure:
        """Map a policy violation to a BackendFailure."""
        return BackendFailure(
            kind=BackendFailureKind.POLICY_VIOLATION,
            message=f"Policy violation: {violation_details}",
            outcome_level=OutcomeLevel.POLICY,
            retryable=False,
            backend_type="claude_sdk",
            worker_role=worker_role,
        )


# =============================================================================
# Main Adapter Class
# =============================================================================

class ClaudeAgentSdkAdapter:
    """
    Adapter for Claude Agent SDK backend.

    This adapter implements the BackendAdapter protocol and provides:
    - Capability negotiation
    - Policy enforcement
    - Normalized output
    - Telemetry collection

    IMPORTANT: This is a SKELETON. Production logic to be added later.
    """

    def __init__(
        self,
        config: ClaudeSdkAdapterConfig | None = None,
        *,
        client_factory: ClaudeSdkClientFactory | None = None,
        clock: Clock | None = None,
        telemetry_sink: TelemetrySink | None = None,
        temp_storage: TempStorage | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.config = config or ClaudeSdkAdapterConfig.default()
        self._client_factory = client_factory
        self._clock = clock or SystemClock()
        self._telemetry_sink = telemetry_sink or NullTelemetrySink()
        self._temp_storage = temp_storage

        # Determine workspace root from parameter or config
        self._workspace_root = workspace_root
        if self._workspace_root is None and self.config.workspace_root:
            self._workspace_root = Path(self.config.workspace_root)

        # Initialize stages
        self._request_builder = RequestBuilder(workspace_root=self._workspace_root)
        self._prompt_assembler = PromptAssembler()
        self._sdk_client_manager = SdkClientManager(self.config, client_factory)
        self._stream_collector = StreamCollector()
        self._output_normalizer = OutputNormalizer()
        self._error_mapper = ErrorMapper()

        # Initialize negotiator
        self._negotiator = CapabilityNegotiator()

    # =========================================================================
    # BackendAdapter Protocol Implementation
    # =========================================================================

    def get_capabilities(self) -> BackendCapabilities:
        """Return declared capabilities of this backend."""
        caps = get_claude_sdk_declared_capabilities()

        return BackendCapabilities(
            # Execution capabilities
            supports_session_context=CapabilityKind.SESSION_CONTEXT in caps,
            supports_agent_loop=CapabilityKind.AGENT_LOOP in caps,
            supports_streaming=CapabilityKind.STREAMING in caps,
            # Tool capabilities
            supports_file_read=CapabilityKind.FILE_READ in caps,
            supports_file_write=CapabilityKind.FILE_WRITE in caps,
            supports_shell=CapabilityKind.SHELL in caps,
            supports_web=CapabilityKind.WEB in caps,
            # Output capabilities
            supports_structured_output_enforcement=CapabilityKind.STRUCTURED_OUTPUT_ENFORCEMENT in caps,
            supports_tool_restriction=CapabilityKind.TOOL_RESTRICTION in caps,
            supports_model_override=CapabilityKind.MODEL_OVERRIDE in caps,
            # Metadata
            backend_type="claude_sdk",
            backend_version=None,  # Will be populated when SDK is integrated
        )

    def check_capability_compatibility(
        self,
        required_capabilities: set[str],
    ) -> tuple[bool, list[str]]:
        """Check if backend supports required capabilities."""
        capabilities = self.get_capabilities()
        missing: list[str] = []

        for cap_name in required_capabilities:
            cap_value = getattr(capabilities, cap_name, None)
            if cap_value is not True:
                missing.append(cap_name)

        return (len(missing) == 0, missing)

    def run_with_backend_contract(
        self,
        worker_role: str,
        worker_input: dict[str, Any],
        policy_envelope: BackendPolicyEnvelope,
    ) -> BackendInvocationResult:
        """
        Execute one worker invocation using the BackendAdapter contract.

        This is the main entry point for all invocations.
        """
        start_time = self._clock.timestamp()

        # Step 1: Validate policy envelope
        validation_errors = validate_policy_envelope(policy_envelope)
        if validation_errors:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(
                    ValueError(f"Invalid policy envelope: {validation_errors}"),
                    worker_role,
                    OutcomeLevel.POLICY,
                ),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

        # Step 2: Negotiate capabilities
        declared_caps = get_claude_sdk_declared_capabilities()
        allowed_caps = policy_envelope_to_allowed_capabilities(
            allow_file_read=policy_envelope.allow_file_read,
            allow_file_write=policy_envelope.allow_file_write,
            allow_shell=policy_envelope.allow_shell,
            allow_web=policy_envelope.allow_web,
        )

        negotiation = self._negotiator.negotiate(
            worker_role=worker_role,
            declared_capabilities=declared_caps,
            policy_allowed_capabilities=allowed_caps,
            backend_type="claude_sdk",
        )

        if not negotiation.success:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_policy_violation(
                    worker_role,
                    f"Missing capabilities: {[c.value for c in negotiation.missing_capabilities]}",
                ),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

        # Step 3: Build invocation spec
        try:
            spec = self._request_builder.build_spec(
                worker_role=worker_role,
                worker_input=worker_input,
                policy_envelope=policy_envelope,
            )
        except Exception as e:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(e, worker_role, OutcomeLevel.SEMANTIC),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

        # Step 4: Assemble prompt
        try:
            prompt = self._prompt_assembler.assemble(spec, policy_envelope)
        except Exception as e:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(e, worker_role, OutcomeLevel.SEMANTIC),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

        # Step 5-8: Invoke actual SDK
        try:
            result = self._invoke_sdk(
                prompt=prompt,
                worker_role=worker_role,
                worker_input=worker_input,
                policy_envelope=policy_envelope,
                start_time=start_time,
            )
            return result
        except Exception as e:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(e, worker_role, OutcomeLevel.SEMANTIC),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

    def _invoke_sdk(
        self,
        prompt: str,
        worker_role: str,
        worker_input: dict[str, Any],
        policy_envelope: BackendPolicyEnvelope,
        start_time: float,
    ) -> BackendInvocationResult:
        """Invoke the actual Claude SDK."""
        import json
        import os

        try:
            import anthropic
        except ImportError:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(
                    ImportError("anthropic package not installed. Run: pip install anthropic"),
                    worker_role,
                    OutcomeLevel.TRANSPORT,
                ),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

        # Get API key - check multiple env vars
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY") or
            os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
        if not api_key:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(
                    ValueError("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN not set"),
                    worker_role,
                    OutcomeLevel.TRANSPORT,
                ),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

        # Get base URL (for custom endpoints)
        base_url = os.environ.get("ANTHROPIC_BASE_URL")

        # Create client
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        client = anthropic.Anthropic(**client_kwargs)

        # Determine model - prefer env var, then config, then default
        model = (
            os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL") or
            self.config.model_name or
            "claude-sonnet-4-20250514"
        )

        # System prompt for structured output
        system_prompt = (
            "You are a code auditor. Respond ONLY with valid JSON matching the expected schema. "
            "Do not include markdown fences or prose outside the JSON."
        )

        try:
            # Make the API call
            message = client.messages.create(
                model=model,
                max_tokens=self.config.max_output_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )

            duration = self._clock.timestamp() - start_time

            # Extract text from response
            raw_text = ""
            for block in message.content:
                if hasattr(block, "text"):
                    raw_text += block.text

            # Normalize output
            normalized = self._output_normalizer.normalize(
                raw_output=raw_text,
                worker_role=worker_role,
                worker_input=worker_input,
            )

            # Build success result
            return BackendInvocationResult(
                success=True,
                payload=normalized.payload,
                candidate_events=normalized.candidate_events,
                telemetry=BackendTelemetry(
                    backend_type="claude_sdk",
                    model=model,
                    duration_seconds=duration,
                    policy_profile=policy_envelope.policy_profile_name,
                    outcome_level_reached=OutcomeLevel.SEMANTIC,
                    metadata={
                        "input_tokens": message.usage.input_tokens if hasattr(message, "usage") else 0,
                        "output_tokens": message.usage.output_tokens if hasattr(message, "usage") else 0,
                        "parse_warnings": normalized.parse_warnings,
                        "raw_output": raw_text,
                    },
                ),
            )

        except anthropic.APIError as e:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(e, worker_role, OutcomeLevel.TRANSPORT),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )
        except Exception as e:
            return self._create_failure_result(
                worker_role=worker_role,
                failure=self._error_mapper.map_error(e, worker_role, OutcomeLevel.SEMANTIC),
                start_time=start_time,
                policy_envelope=policy_envelope,
            )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _create_failure_result(
        self,
        worker_role: str,
        failure: BackendFailure,
        start_time: float,
        policy_envelope: BackendPolicyEnvelope,
    ) -> BackendInvocationResult:
        """Create a failure result."""
        duration = self._clock.timestamp() - start_time

        return BackendInvocationResult(
            success=False,
            error=failure,
            telemetry=BackendTelemetry(
                backend_type="claude_sdk",
                duration_seconds=duration,
                policy_profile=policy_envelope.policy_profile_name,
                outcome_level_reached=failure.outcome_level,
            ),
        )

    def _create_skeleton_result(
        self,
        worker_role: str,
        worker_input: dict[str, Any],
        prompt: str,
        start_time: float,
        policy_envelope: BackendPolicyEnvelope,
    ) -> BackendInvocationResult:
        """Create a skeleton result (placeholder for actual SDK invocation)."""
        duration = self._clock.timestamp() - start_time

        # SKELETON: This would be replaced with actual SDK invocation
        return BackendInvocationResult(
            success=True,
            payload={"skeleton": True, "worker_role": worker_role},
            candidate_events=[],
            telemetry=BackendTelemetry(
                backend_type="claude_sdk",
                model=self.config.model_name,
                duration_seconds=duration,
                policy_profile=policy_envelope.policy_profile_name,
                outcome_level_reached=OutcomeLevel.SEMANTIC,
                metadata={"skeleton_mode": True, "prompt_length": len(prompt)},
            ),
        )

    def _emit_telemetry(self, event: dict[str, Any]) -> None:
        """Emit a telemetry event."""
        if self.config.telemetry_enabled:
            self._telemetry_sink.emit(event)

    # =========================================================================
    # Compatibility Method - matches CodexAdapter interface for CLI
    # =========================================================================

    def run_with_result(
        self,
        worker_role: str,
        worker_input_source: str | Path,
    ) -> "CodexRunResult":
        """
        Compatibility method that matches CodexAdapter.run_with_result() interface.

        This method allows ClaudeAgentSdkAdapter to be used as a drop-in
        replacement in the CLI without code changes.

        Args:
            worker_role: Role of worker to execute
            worker_input_source: Path to worker input JSON file

        Returns:
            CodexRunResult with candidate_events and invocation metadata

        IMPORTANT: This method bridges the old CodexAdapter interface
        to the new BackendAdapter contract. It loads the worker input
        from a file, creates a default policy envelope, and delegates to
        run_with_backend_contract().
        """
        import json
        from pathlib import Path

        from runtime.adapters.base import BackendPolicyEnvelope

        # Load worker input from file if path provided
        worker_input: dict[str, Any]
        if isinstance(worker_input_source, (str, Path)):
            slice_path = Path(worker_input_source)
            with slice_path.open("r", encoding="utf-8") as f:
                worker_input = json.load(f)
        else:
            # Already a dict
            worker_input = worker_input_source

        # Create a default policy envelope based on worker role
        policy_envelope = self._create_default_policy_envelope(worker_role)

        # Delegate to the contract method
        result = self.run_with_backend_contract(
            worker_role=worker_role,
            worker_input=worker_input,
            policy_envelope=policy_envelope,
        )

        # Convert BackendInvocationResult to CodexRunResult
        return self._convert_to_codex_result(
            worker_role=worker_role,
            worker_input=worker_input,
            result=result,
        )

    def _create_default_policy_envelope(
        self,
        worker_role: str,
    ) -> BackendPolicyEnvelope:
        """
        Create a default policy envelope for the worker role.

        This provides sensible defaults that allow the worker to function.
        """
        working_dir = self.config.working_directory or "."

        # Base policy allows file read but restricts writes/shell/web
        if worker_role == "Reader":
            return BackendPolicyEnvelope(
                allowed_working_directory=working_dir,
                allow_file_read=True,
                allow_file_write=False,
                allow_shell=False,
                allow_web=False,
                policy_profile_name="reader_default",
            )
        elif worker_role == "Verifier":
            return BackendPolicyEnvelope(
                allowed_working_directory=working_dir,
                allow_file_read=True,
                allow_file_write=False,
                allow_shell=False,
                allow_web=False,
                policy_profile_name="verifier_default",
            )
        elif worker_role == "IssueComposer":
            return BackendPolicyEnvelope(
                allowed_working_directory=working_dir,
                allow_file_read=False,  # IssueComposer works with provided context
                allow_file_write=False,
                allow_shell=False,
                allow_web=False,
                policy_profile_name="issue_composer_default",
            )
        elif worker_role == "CandidateGenerator":
            return BackendPolicyEnvelope(
                allowed_working_directory=working_dir,
                allow_file_read=True,
                allow_file_write=False,
                allow_shell=False,
                allow_web=False,
                policy_profile_name="candidate_generator_default",
            )
        else:
            # Default: read-only
            return BackendPolicyEnvelope(
                allowed_working_directory=working_dir,
                allow_file_read=True,
                allow_file_write=False,
                allow_shell=False,
                allow_web=False,
                policy_profile_name="default",
            )

    @staticmethod
    def _ensure_transport_format(event: dict[str, Any]) -> dict[str, Any]:
        """Wrap a Claude-format candidate event into the transport format
        expected by ``CodexAdapter._normalize_transport_candidate_event``.

        Claude (unlike Codex) does not receive transport-envelope instructions,
        so its output often looks like::

            {"event_type": "observation.proposed", "claim": "...",
             "evidence": {"file_path": "app", ...}}

        The normalizer requires::

            {"event_type": "observation.proposed",
             "payload": {"claim": "...", "evidence": [...]}}
        """
        if not isinstance(event, dict):
            return event

        # Already in transport format (has a payload dict) or fully internal.
        if isinstance(event.get("payload"), dict):
            return event

        event_type = event.get("event_type") or event.get("type")
        if not isinstance(event_type, str) or not event_type:
            return event

        # Build a payload dict from non-metadata keys.
        metadata_keys = {
            "event_type", "type",
            "confidence", "supporting_observations",
        }
        payload: dict[str, Any] = {}
        for key, value in event.items():
            if key not in metadata_keys:
                payload[key] = value

        # Normalize evidence: single object → list of source refs.
        evidence = payload.get("evidence")
        if isinstance(evidence, dict):
            payload["evidence"] = [evidence]

        return {"event_type": event_type, "payload": payload}

    def _convert_to_codex_result(
        self,
        worker_role: str,
        worker_input: dict[str, Any],
        result: BackendInvocationResult,
    ) -> "CodexRunResult":
        """
        Convert BackendInvocationResult to CodexRunResult.

        This bridges the new BackendAdapter contract to the legacy CodexRunResult
        type that the CLI expects.

        Raw candidate events from the LLM are normalized into internal format
        using CodexAdapter._normalize_transport_candidate_event to ensure they
        include the acceptance envelope and entity metadata required by
        process_candidate_events.
        """
        import json
        from datetime import datetime, timezone

        from runtime.adapters.codex_adapter import CodexAdapter, CodexRunResult

        occurred_at = datetime.now(timezone.utc).isoformat()
        normalized_events: list[dict[str, Any]] = []
        for index, event in enumerate(result.candidate_events):
            try:
                transport_event = self._ensure_transport_format(event)
                normalized = CodexAdapter._normalize_transport_candidate_event(
                    worker_role,
                    worker_input,
                    transport_event,
                    index=index,
                    occurred_at=occurred_at,
                )
                normalized_events.append(normalized)
            except Exception:
                # Preserve unparseable events for debugging; they'll be rejected
                # by process_candidate_events with a clear error.
                normalized_events.append(json.loads(json.dumps(event)))

        return CodexRunResult(
            worker_role=worker_role,
            worker_input=worker_input,
            prompt=result.telemetry.metadata.get("prompt", "") if result.telemetry else "",
            raw_output=result.telemetry.metadata.get("raw_output", "") if result.telemetry else "",
            normalized_output=result.payload or {},
            candidate_events=normalized_events,
            input_digest=result.input_digest or "",
            output_digest=result.output_digest or "",
            prompt_digest=result.telemetry.metadata.get("prompt_digest", "") if result.telemetry else "",
            raw_output_digest=result.telemetry.metadata.get("raw_output_digest", "") if result.telemetry else "",
            invocation_metadata=result.telemetry.metadata if result.telemetry and result.telemetry.metadata else {},
        )
