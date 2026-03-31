"""Test: Transport envelope instructions in Claude prompt.

Verify that the PromptAssembler includes transport format instructions
so Claude knows to produce candidate_events with the right structure.
"""

from __future__ import annotations

from runtime.adapters.base import BackendPolicyEnvelope
from runtime.adapters.claude_agent_sdk_adapter import (
    ClaudeAgentSdkAdapter,
    InvocationSpec,
    PromptAssembler,
)


class TestTransportInstructionsInPrompt:
    def _make_spec(self, worker_role: str = "Reader") -> InvocationSpec:
        return InvocationSpec(
            worker_role=worker_role,
            worker_input={"task": {"id": "task_1", "audit_id": "audit_1"}},
            task_prompt="Analyze the target path.",
            constraints=["No issue creation"],
        )

    def _make_policy(self) -> BackendPolicyEnvelope:
        return BackendPolicyEnvelope(
            allowed_working_directory=".",
            allow_file_read=True,
            policy_profile_name="reader_default",
        )

    def test_output_format_contains_transport_instructions(self):
        assembler = PromptAssembler()
        spec = self._make_spec()
        policy = self._make_policy()
        prompt = assembler.assemble(spec, policy)

        assert "candidate_events" in prompt
        assert "event_type" in prompt
        assert "payload" in prompt
        assert "claim" in prompt
        assert "evidence" in prompt
        assert "file_path" in prompt
        assert "line_start" in prompt
        assert "observation.proposed" in prompt

    def test_output_format_includes_question_opened_rules(self):
        assembler = PromptAssembler()
        spec = self._make_spec()
        policy = self._make_policy()
        prompt = assembler.assemble(spec, policy)

        assert "question.opened" in prompt
        assert "payload.question" in prompt

    def test_output_format_includes_evidence_array_rule(self):
        assembler = PromptAssembler()
        spec = self._make_spec()
        policy = self._make_policy()
        prompt = assembler.assemble(spec, policy)

        # Must mention evidence as array
        assert "payload.evidence" in prompt
        assert "CRITICAL" in prompt  # Evidence requirement emphasis

    def test_get_output_format_returns_non_empty(self):
        result = PromptAssembler()._get_output_format_instructions("Reader")
        assert isinstance(result, str)
        assert len(result) > 100  # Substantial instructions, not a placeholder

    def test_verifier_role_gets_transport_instructions(self):
        assembler = PromptAssembler()
        spec = self._make_spec(worker_role="Verifier")
        policy = self._make_policy()
        prompt = assembler.assemble(spec, policy)
        assert "candidate_events" in prompt
        assert "event_type" in prompt
