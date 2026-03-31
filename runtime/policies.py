from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


POLICIES_SCHEMA_VERSION = "1.0.0"
VALID_PROFILES = ("strict_security", "low_noise", "exploratory")
DEFAULT_PROFILE = "low_noise"
EVIDENCE_CLASS_HIERARCHY = (
    "direct_code_fact",
    "derived_structural_fact",
    "inferred_hypothesis",
    "blocked_verification",
)


class PolicyError(Exception):
    """Base error for policy loading and validation failures."""


@dataclass(frozen=True)
class QuestionEmissionPolicy:
    emit_on_ambiguity: bool
    emit_on_security_concern: bool
    emit_on_unclear_intent: bool
    suppress_near_duplicates: bool
    max_questions_per_slice: int


@dataclass(frozen=True)
class TaskExpansionPolicy:
    verify_claim_per_observation: bool
    compose_issue_for_verified: bool
    defer_on_budget_soft: bool


@dataclass(frozen=True)
class ComposeIssueBudgetPolicy:
    max_per_audit: int
    max_per_source_path: int
    allow_inferred_evidence: bool


@dataclass(frozen=True)
class VerifyClaimBudgetPolicy:
    """Budget controls for verify_claim task expansion.

    Prevents exponential task graph growth by limiting:
    - Total verify_claim tasks per audit
    - Tasks targeting the same observation (deduplication)
    - Fan-out per iteration (explosion prevention)
    """
    max_per_audit: int
    max_per_observation: int
    max_follow_up_per_iteration: int


@dataclass(frozen=True)
class IssueCompositionPolicy:
    require_rule_binding_for_severity: bool
    include_low_confidence: bool
    min_evidence_class: str


@dataclass(frozen=True)
class CandidateRoutingPolicy:
    """Policy for routing candidates into verification tasks.

    Controls how aggressively candidates generate follow-up work.
    Candidates are non-authoritative and must be verified.
    """
    max_candidates_per_audit: int
    max_verify_tasks_per_run: int
    max_module_scan_per_cross_file: int
    max_total_tasks_per_candidate: int
    defer_low_confidence: bool
    suppress_near_duplicate_files: bool
    route_risk_candidates: bool
    route_policy_candidates: bool
    route_cross_file_correlations: bool
    route_verification_targets: bool


@dataclass(frozen=True)
class AuditPolicy:
    profile_name: str
    description: str
    question_emission: QuestionEmissionPolicy
    task_expansion: TaskExpansionPolicy
    verify_claim_budget: VerifyClaimBudgetPolicy
    compose_issue_budget: ComposeIssueBudgetPolicy
    issue_composition: IssueCompositionPolicy
    candidate_routing: CandidateRoutingPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "description": self.description,
            "question_emission": {
                "emit_on_ambiguity": self.question_emission.emit_on_ambiguity,
                "emit_on_security_concern": self.question_emission.emit_on_security_concern,
                "emit_on_unclear_intent": self.question_emission.emit_on_unclear_intent,
                "suppress_near_duplicates": self.question_emission.suppress_near_duplicates,
                "max_questions_per_slice": self.question_emission.max_questions_per_slice,
            },
            "task_expansion": {
                "verify_claim_per_observation": self.task_expansion.verify_claim_per_observation,
                "compose_issue_for_verified": self.task_expansion.compose_issue_for_verified,
                "defer_on_budget_soft": self.task_expansion.defer_on_budget_soft,
            },
            "verify_claim_budget": {
                "max_per_audit": self.verify_claim_budget.max_per_audit,
                "max_per_observation": self.verify_claim_budget.max_per_observation,
                "max_follow_up_per_iteration": self.verify_claim_budget.max_follow_up_per_iteration,
            },
            "compose_issue_budget": {
                "max_per_audit": self.compose_issue_budget.max_per_audit,
                "max_per_source_path": self.compose_issue_budget.max_per_source_path,
                "allow_inferred_evidence": self.compose_issue_budget.allow_inferred_evidence,
            },
            "issue_composition": {
                "require_rule_binding_for_severity": self.issue_composition.require_rule_binding_for_severity,
                "include_low_confidence": self.issue_composition.include_low_confidence,
                "min_evidence_class": self.issue_composition.min_evidence_class,
            },
            "candidate_routing": {
                "max_candidates_per_audit": self.candidate_routing.max_candidates_per_audit,
                "max_verify_tasks_per_run": self.candidate_routing.max_verify_tasks_per_run,
                "max_module_scan_per_cross_file": self.candidate_routing.max_module_scan_per_cross_file,
                "max_total_tasks_per_candidate": self.candidate_routing.max_total_tasks_per_candidate,
                "defer_low_confidence": self.candidate_routing.defer_low_confidence,
                "suppress_near_duplicate_files": self.candidate_routing.suppress_near_duplicate_files,
                "route_risk_candidates": self.candidate_routing.route_risk_candidates,
                "route_policy_candidates": self.candidate_routing.route_policy_candidates,
                "route_cross_file_correlations": self.candidate_routing.route_cross_file_correlations,
                "route_verification_targets": self.candidate_routing.route_verification_targets,
            },
        }


class PolicyStore:
    """Load and provide access to audit policy profiles."""

    def __init__(
        self,
        root_dir: str | Path,
        config_dir: str | Path = "config",
        policies_name: str = "policies.yaml",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.policies_path = (self.root_dir / config_dir / policies_name).resolve()
        self._policies_data: dict[str, Any] | None = None
        self._policy_cache: dict[str, AuditPolicy] = {}

    def load_policies(self) -> dict[str, Any]:
        """Load and validate the policies configuration file."""
        if self._policies_data is not None:
            return self._policies_data

        if not self.policies_path.exists():
            raise PolicyError(f"Policies configuration file does not exist: {self.policies_path}")

        try:
            with self.policies_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise PolicyError(f"Invalid YAML in policies file: {self.policies_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise PolicyError(f"Policies file must contain a YAML object: {self.policies_path}")

        if data.get("schema_version") != POLICIES_SCHEMA_VERSION:
            raise PolicyError(
                f"Unsupported policies schema version '{data.get('schema_version')}'. "
                f"Expected: {POLICIES_SCHEMA_VERSION}"
            )

        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            raise PolicyError("Policies file must contain a 'profiles' object.")

        for profile_name in VALID_PROFILES:
            if profile_name not in profiles:
                raise PolicyError(f"Missing required profile: {profile_name}")

        self._policies_data = data
        return data

    def get_policy(self, profile_name: str | None = None) -> AuditPolicy:
        """Get an audit policy by profile name, or the default if not specified."""
        effective_profile = profile_name or DEFAULT_PROFILE

        if effective_profile not in VALID_PROFILES:
            raise PolicyError(
                f"Invalid profile '{effective_profile}'. "
                f"Valid profiles: {', '.join(VALID_PROFILES)}"
            )

        if effective_profile in self._policy_cache:
            return self._policy_cache[effective_profile]

        data = self.load_policies()
        profile_data = data["profiles"][effective_profile]

        policy = AuditPolicy(
            profile_name=effective_profile,
            description=profile_data.get("description", "").strip(),
            question_emission=QuestionEmissionPolicy(
                emit_on_ambiguity=bool(profile_data["question_emission"].get("emit_on_ambiguity", True)),
                emit_on_security_concern=bool(profile_data["question_emission"].get("emit_on_security_concern", True)),
                emit_on_unclear_intent=bool(profile_data["question_emission"].get("emit_on_unclear_intent", False)),
                suppress_near_duplicates=bool(profile_data["question_emission"].get("suppress_near_duplicates", False)),
                max_questions_per_slice=int(profile_data["question_emission"].get("max_questions_per_slice", 10)),
            ),
            task_expansion=TaskExpansionPolicy(
                verify_claim_per_observation=bool(profile_data["task_expansion"].get("verify_claim_per_observation", True)),
                compose_issue_for_verified=bool(profile_data["task_expansion"].get("compose_issue_for_verified", True)),
                defer_on_budget_soft=bool(profile_data["task_expansion"].get("defer_on_budget_soft", False)),
            ),
            verify_claim_budget=VerifyClaimBudgetPolicy(
                max_per_audit=int(profile_data.get("verify_claim_budget", {}).get("max_per_audit", 100)),
                max_per_observation=int(profile_data.get("verify_claim_budget", {}).get("max_per_observation", 1)),
                max_follow_up_per_iteration=int(profile_data.get("verify_claim_budget", {}).get("max_follow_up_per_iteration", 5)),
            ),
            compose_issue_budget=ComposeIssueBudgetPolicy(
                max_per_audit=int(profile_data["compose_issue_budget"].get("max_per_audit", 24)),
                max_per_source_path=int(profile_data["compose_issue_budget"].get("max_per_source_path", 4)),
                allow_inferred_evidence=bool(profile_data["compose_issue_budget"].get("allow_inferred_evidence", False)),
            ),
            issue_composition=IssueCompositionPolicy(
                require_rule_binding_for_severity=bool(profile_data["issue_composition"].get("require_rule_binding_for_severity", True)),
                include_low_confidence=bool(profile_data["issue_composition"].get("include_low_confidence", False)),
                min_evidence_class=str(profile_data["issue_composition"].get("min_evidence_class", "derived_structural_fact")),
            ),
            candidate_routing=CandidateRoutingPolicy(
                max_candidates_per_audit=int(profile_data.get("candidate_routing", {}).get("max_candidates_per_audit", 50)),
                max_verify_tasks_per_run=int(profile_data.get("candidate_routing", {}).get("max_verify_tasks_per_run", 20)),
                max_module_scan_per_cross_file=int(profile_data.get("candidate_routing", {}).get("max_module_scan_per_cross_file", 3)),
                max_total_tasks_per_candidate=int(profile_data.get("candidate_routing", {}).get("max_total_tasks_per_candidate", 3)),
                defer_low_confidence=bool(profile_data.get("candidate_routing", {}).get("defer_low_confidence", True)),
                suppress_near_duplicate_files=bool(profile_data.get("candidate_routing", {}).get("suppress_near_duplicate_files", True)),
                route_risk_candidates=bool(profile_data.get("candidate_routing", {}).get("route_risk_candidates", True)),
                route_policy_candidates=bool(profile_data.get("candidate_routing", {}).get("route_policy_candidates", True)),
                route_cross_file_correlations=bool(profile_data.get("candidate_routing", {}).get("route_cross_file_correlations", True)),
                route_verification_targets=bool(profile_data.get("candidate_routing", {}).get("route_verification_targets", True)),
            ),
        )

        self._policy_cache[effective_profile] = policy
        return policy

    def list_profiles(self) -> list[str]:
        """List available profile names."""
        return list(VALID_PROFILES)

    def get_evidence_class_hierarchy(self) -> tuple[str, ...]:
        """Get the evidence class hierarchy from strongest to weakest."""
        data = self.load_policies()
        hierarchy = data.get("evidence_class_hierarchy", list(EVIDENCE_CLASS_HIERARCHY))
        return tuple(hierarchy)


def evidence_class_strength(evidence_class: str, hierarchy: tuple[str, ...] = EVIDENCE_CLASS_HIERARCHY) -> int:
    """Return the strength rank of an evidence class (lower is stronger)."""
    try:
        return hierarchy.index(evidence_class)
    except ValueError:
        return len(hierarchy)


def meets_minimum_evidence_class(
    evidence_class: str,
    minimum_class: str,
    hierarchy: tuple[str, ...] = EVIDENCE_CLASS_HIERARCHY,
) -> bool:
    """Check if an evidence class meets or exceeds the minimum required strength."""
    return evidence_class_strength(evidence_class, hierarchy) <= evidence_class_strength(minimum_class, hierarchy)
