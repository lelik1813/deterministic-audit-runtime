from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.evidence import (
    ALLOWED_FINDING_EVIDENCE_CLASSES,
    count_evidence_classes,
    count_evidence_origins,
    present_evidence_classes,
)
from runtime.secret_redaction import redact_report


REPORT_SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION_V1_2 = "1.1.0"  # Includes optional candidate appendix

# Candidate types and outcomes for appendix
CANDIDATE_TYPES = ("risk_candidate", "policy_candidate", "cross_file_correlation", "verification_target")
CANDIDATE_OUTCOMES = ("proposed", "routed_to_verify", "rejected", "resolved_promoted")

# Audit lifecycle statuses recognized by report compiler.
# Canonical schema lifecycle: initialized -> in_progress -> completed/cancelled.
# Backward-compatible aliases are preserved for older workspaces.
ALLOWED_AUDIT_STATUSES = {
    "initialized",
    "in_progress",
    "reported",
    "completed",
    "cancelled",
    "failed",
    "accepted",
    "scanning",
    "analyzed",
}


class ReportCompileError(Exception):
    """Raised when a deterministic report cannot be compiled from canonical state."""


@dataclass(frozen=True)
class ReportCompileResult:
    audit_id: str
    report_id: str
    report_path: Path


def _normalize(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class ReportCompiler:
    """Compile deterministic audit reports from accepted canonical state only."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        state_dir: str | Path = "state",
        reports_dir: str | Path = "reports",
        canonical_state_name: str = "canonical_state.json",
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.state_dir = (self.root_dir / state_dir).resolve()
        self.reports_dir = (self.root_dir / reports_dir).resolve()
        self.canonical_state_path = (self.state_dir / canonical_state_name).resolve()

        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def build_report(
        self,
        canonical_state: dict[str, Any] | None = None,
        *,
        include_candidate_appendix: bool = False,
    ) -> dict[str, Any]:
        state = _normalize(canonical_state) if canonical_state is not None else self._load_canonical_state()
        self._validate_state_shape(state)

        audit = state["audit"]
        if not isinstance(audit, dict):
            raise ReportCompileError("Canonical state must contain an accepted audit root.")

        audit_id = audit.get("id")
        if not isinstance(audit_id, str) or not audit_id:
            raise ReportCompileError("Canonical audit must expose a non-empty id.")

        verified_observations = self._collect_verified_observations(state["observations"], audit_id)
        questions = self._collect_questions(state["questions"], audit_id)
        contradictions = self._collect_contradictions(state["contradictions"], audit_id)
        decisions = self._collect_decisions(state["decisions"], audit_id)
        findings = self._collect_findings(
            issues=state["issues"],
            audit_id=audit_id,
            verified_observations=verified_observations,
            questions=questions,
            contradictions=contradictions,
        )
        findings, suppression_records = self._merge_with_observation_mapped_findings(
            findings=findings,
            verified_observations=verified_observations,
        )

        state_digest = hashlib.sha256(_canonical_json(state).encode("ascii")).hexdigest()
        report_id = f"report_{state_digest[:16]}"
        open_questions = [question for question in questions.values() if question["status"] == "open"]
        report_populated = bool(
            findings
            or verified_observations
            or open_questions
            or contradictions
            or decisions
        )
        self._validate_report_audit_status(audit, report_populated=report_populated)

        # Build main report (truth-bearing only)
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_id": report_id,
            "source_audit_id": audit_id,
            "source_state_digest": state_digest,
            "audit": self._build_audit_section(audit),
            "summary": {
                "finding_count": len(findings),
                "verified_observation_count": len(verified_observations),
                "open_question_count": len(open_questions),
                "contradiction_count": len(contradictions),
                "decision_count": len(decisions),
                "verified_observation_evidence_class_counts": count_evidence_classes(
                    verified_observations.values()
                ),
                "verified_observation_evidence_origin_counts": count_evidence_origins(
                    verified_observations.values()
                ),
            },
            "findings": findings,
            "suppression_records": suppression_records,
            "verified_observations": list(verified_observations.values()),
            "open_questions": open_questions,
            "contradictions": list(contradictions.values()),
            "decisions": list(decisions.values()),
        }

        # Optionally include candidate appendix (v1.2)
        # IMPORTANT: Candidate appendix is NON-AUTHORITATIVE and does NOT affect findings
        if include_candidate_appendix:
            candidates = state.get("candidates", {})
            candidate_appendix = self._build_candidate_appendix(candidates, audit_id)
            if candidate_appendix is not None:
                report["schema_version"] = REPORT_SCHEMA_VERSION_V1_2
                report["candidate_appendix"] = candidate_appendix

        self._validate_compiled_report_consistency(report)
        return report

    def write_report(
        self,
        canonical_state: dict[str, Any] | None = None,
        *,
        report_name: str | None = None,
        include_candidate_appendix: bool = False,
    ) -> ReportCompileResult:
        report = self.build_report(
            canonical_state=canonical_state,
            include_candidate_appendix=include_candidate_appendix,
        )
        # Redact secrets before persistence
        redacted_report = redact_report(report)
        report_path = self.reports_dir / (report_name or f"report.{redacted_report['source_audit_id']}.json")
        serialized = json.dumps(redacted_report, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        report_path.write_text(serialized, encoding="utf-8", newline="\n")
        return ReportCompileResult(
            audit_id=redacted_report["source_audit_id"],
            report_id=redacted_report["report_id"],
            report_path=report_path,
        )

    def _load_canonical_state(self) -> dict[str, Any]:
        if not self.canonical_state_path.exists():
            raise ReportCompileError(f"Canonical state file does not exist: {self.canonical_state_path}")
        with self.canonical_state_path.open("r", encoding="utf-8") as handle:
            try:
                return json.load(handle)
            except json.JSONDecodeError as exc:
                raise ReportCompileError(
                    f"Canonical state is not valid JSON: {self.canonical_state_path}"
                ) from exc

    @staticmethod
    def _validate_state_shape(state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise ReportCompileError("Canonical state must be a JSON object.")

        required_maps = ("observations", "issues", "questions", "contradictions", "decisions")
        for field_name in required_maps:
            if not isinstance(state.get(field_name), dict):
                raise ReportCompileError(f"Canonical state field '{field_name}' must be an object map.")

    @staticmethod
    def _build_audit_section(audit: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": audit["id"],
            "status": audit["status"],
            "title": audit.get("title"),
            "target": _normalize(audit["target"]),
            "current_snapshot_ref": audit.get("current_snapshot_ref"),
        }

    @staticmethod
    def _validate_report_audit_status(audit: dict[str, Any], *, report_populated: bool) -> None:
        raw_status = audit.get("status")
        if not isinstance(raw_status, str) or not raw_status:
            raise ReportCompileError("Canonical audit must expose a non-empty status.")
        if raw_status not in ALLOWED_AUDIT_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_AUDIT_STATUSES))
            raise ReportCompileError(
                f"Unsupported audit.status '{raw_status}'. Allowed statuses: {allowed}."
            )
        # Critical guardrail: populated reports must never remain in initialized state.
        if report_populated and raw_status == "initialized":
            raise ReportCompileError(
                "Cannot compile populated report while audit.status='initialized'. "
                "Advance status to at least in_progress before report emission."
            )

    def _collect_verified_observations(
        self,
        observations: dict[str, Any],
        audit_id: str,
    ) -> dict[str, dict[str, Any]]:
        verified: dict[str, dict[str, Any]] = {}
        for observation_id in sorted(observations):
            observation = observations[observation_id]
            if observation.get("audit_id") != audit_id or observation.get("status") != "verified":
                continue
            verified[observation_id] = self._build_verified_observation_entry(observation)
        return verified

    def _collect_questions(
        self,
        questions: dict[str, Any],
        audit_id: str,
    ) -> dict[str, dict[str, Any]]:
        collected: dict[str, dict[str, Any]] = {}
        for question_id in sorted(questions):
            question = questions[question_id]
            if question.get("audit_id") != audit_id:
                continue
            collected[question_id] = {
                "question_id": question["id"],
                "status": question["status"],
                "prompt": question["prompt"],
                "context": question["context"],
                "answer": question["answer"],
                "related_entity_refs": self._sorted_entity_refs(question.get("related_entity_refs", [])),
            }
        return collected

    def _collect_contradictions(
        self,
        contradictions: dict[str, Any],
        audit_id: str,
    ) -> dict[str, dict[str, Any]]:
        collected: dict[str, dict[str, Any]] = {}
        for contradiction_id in sorted(contradictions):
            contradiction = contradictions[contradiction_id]
            if contradiction.get("audit_id") != audit_id:
                continue
            collected[contradiction_id] = {
                "contradiction_id": contradiction["id"],
                "status": contradiction["status"],
                "summary": contradiction["summary"],
                "conflicting_entity_refs": self._sorted_entity_refs(
                    contradiction.get("conflicting_entity_refs", [])
                ),
                "source_refs": self._sorted_source_refs(contradiction.get("source_refs", [])),
            }
        return collected

    def _collect_decisions(
        self,
        decisions: dict[str, Any],
        audit_id: str,
    ) -> dict[str, dict[str, Any]]:
        collected: dict[str, dict[str, Any]] = {}
        for decision_id in sorted(decisions):
            decision = decisions[decision_id]
            if decision.get("audit_id") != audit_id:
                continue
            collected[decision_id] = {
                "decision_id": decision["id"],
                "kind": decision["kind"],
                "summary": decision["summary"],
                "rationale": decision["rationale"],
                "basis_entity_refs": self._sorted_entity_refs(decision.get("basis_entity_refs", [])),
                "source_refs": self._sorted_source_refs(decision.get("source_refs", [])),
            }
        return collected

    def _collect_findings(
        self,
        *,
        issues: dict[str, Any],
        audit_id: str,
        verified_observations: dict[str, dict[str, Any]],
        questions: dict[str, dict[str, Any]],
        contradictions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []

        for issue_id in sorted(issues):
            issue = issues[issue_id]
            if issue.get("audit_id") != audit_id:
                continue
            if issue.get("status") == "rejected":
                continue

            evidence = issue.get("evidence", {})
            observation_ids = evidence.get("observation_ids", [])
            if not isinstance(observation_ids, list) or not observation_ids:
                raise ReportCompileError(
                    f"Issue '{issue_id}' must reference at least one supporting observation."
                )

            supporting_observations: list[dict[str, Any]] = []
            for observation_id in sorted(observation_ids):
                observation = verified_observations.get(observation_id)
                if observation is None:
                    raise ReportCompileError(
                        f"Issue '{issue_id}' references observation '{observation_id}' that is not verified "
                        "in canonical state."
                    )
                evidence_class = observation.get("evidence_class")
                if evidence_class not in ALLOWED_FINDING_EVIDENCE_CLASSES:
                    allowed = ", ".join(sorted(ALLOWED_FINDING_EVIDENCE_CLASSES))
                    raise ReportCompileError(
                        f"Issue '{issue_id}' references observation '{observation_id}' with disallowed "
                        f"evidence_class '{evidence_class}'. Allowed classes: {allowed}."
                    )
                supporting_observations.append(_normalize(observation))

            question_ids = evidence.get("question_ids", [])
            contradiction_ids = evidence.get("contradiction_ids", [])
            open_questions = self._resolve_open_questions(issue_id, question_ids, questions)
            related_contradictions = self._resolve_contradictions(
                issue_id,
                contradiction_ids,
                contradictions,
            )

            findings.append(
                {
                    # New actionable finding identity (additive, backward-compatible)
                    "finding_id": issue.get("finding_id", issue["id"]),
                    "issue_id": issue["id"],
                    "status": issue["status"],
                    "title": issue["title"],
                    "summary": issue["summary"],
                    "severity": issue["severity"],
                    # New actionable fields (additive, backward-compatible)
                    "confidence": issue.get("confidence", "medium"),
                    "impact": issue.get("impact", issue["summary"]),
                    "recommended_fix": issue.get("recommended_fix"),
                    "severity_rule_ref": issue["severity_rule_ref"],
                    "source_observation_ids": [item["observation_id"] for item in supporting_observations],
                    "supporting_observation_ids": [item["observation_id"] for item in supporting_observations],
                    "supporting_evidence_classes": present_evidence_classes(supporting_observations),
                    "supporting_evidence": supporting_observations,
                    "uncertainty": {
                        "open_question_ids": [question["question_id"] for question in open_questions],
                        "open_questions": open_questions,
                        "contradiction_ids": [
                            contradiction["contradiction_id"] for contradiction in related_contradictions
                        ],
                        "contradictions": related_contradictions,
                    },
                }
            )

        return findings

    def _merge_with_observation_mapped_findings(
        self,
        *,
        findings: list[dict[str, Any]],
        verified_observations: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        merged: list[dict[str, Any]] = [_normalize(item) for item in findings]
        suppression_records: list[dict[str, Any]] = []
        covered_observation_ids: set[str] = set()
        existing_finding_ids: set[str] = set()

        for finding in merged:
            existing_finding_id = finding.get("finding_id")
            if isinstance(existing_finding_id, str) and existing_finding_id:
                existing_finding_ids.add(existing_finding_id)

            for observation_id in finding.get("source_observation_ids", []) or []:
                if isinstance(observation_id, str):
                    covered_observation_ids.add(observation_id)
            for observation_id in finding.get("supporting_observation_ids", []) or []:
                if isinstance(observation_id, str):
                    covered_observation_ids.add(observation_id)

        for observation_id in sorted(verified_observations):
            if observation_id in covered_observation_ids:
                continue
            observation = verified_observations[observation_id]
            evidence_class = observation.get("evidence_class")
            if evidence_class not in ALLOWED_FINDING_EVIDENCE_CLASSES:
                suppression_records.append(
                    self._build_suppression_record(
                        observation_id=observation_id,
                        reason_code="policy_suppressed",
                        reason_detail=f"evidence_class_not_allowed:{evidence_class}",
                    )
                )
                continue

            synthesized_finding_id = f"finding_obs_{observation_id}"
            if synthesized_finding_id in existing_finding_ids:
                suppression_records.append(
                    self._build_suppression_record(
                        observation_id=observation_id,
                        reason_code="duplicate_of",
                        reason_detail=f"finding_id_collision:{synthesized_finding_id}",
                        canonical_finding_id=synthesized_finding_id,
                    )
                )
                continue

            statement = observation.get("statement", "")
            severity = self._derive_severity_from_observation(observation)
            confidence = self._derive_confidence_from_observation(observation)

            synthesized = {
                "finding_id": synthesized_finding_id,
                "issue_id": f"derived:{observation_id}",
                "status": "open",
                "title": self._derive_title_from_statement(statement),
                "summary": statement,
                "severity": severity,
                "confidence": confidence,
                "impact": statement,
                "recommended_fix": None,
                "severity_rule_ref": "observation_mapper.default_v1",
                "source_observation_ids": [observation_id],
                "supporting_observation_ids": [observation_id],
                "supporting_evidence_classes": [evidence_class],
                "supporting_evidence": [_normalize(observation)],
                "uncertainty": {
                    "open_question_ids": [],
                    "open_questions": [],
                    "contradiction_ids": [],
                    "contradictions": [],
                },
            }
            merged.append(synthesized)
            existing_finding_ids.add(synthesized_finding_id)
            covered_observation_ids.add(observation_id)

        return merged, suppression_records

    @staticmethod
    def _build_suppression_record(
        *,
        observation_id: str,
        reason_code: str,
        reason_detail: str,
        canonical_finding_id: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "suppression_id": f"sup_{observation_id}",
            "observation_id": observation_id,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "status": "suppressed",
        }
        if canonical_finding_id:
            record["canonical_finding_id"] = canonical_finding_id
        return record

    @staticmethod
    def _validate_compiled_report_consistency(report: dict[str, Any]) -> None:
        summary = report.get("summary")
        if not isinstance(summary, dict):
            raise ReportCompileError("Compiled report must contain summary object.")

        findings = report.get("findings")
        verified_observations = report.get("verified_observations")
        open_questions = report.get("open_questions")
        contradictions = report.get("contradictions")
        decisions = report.get("decisions")
        suppression_records = report.get("suppression_records", [])

        if not isinstance(findings, list):
            raise ReportCompileError("Compiled report field 'findings' must be a JSON array.")
        if not isinstance(verified_observations, list):
            raise ReportCompileError("Compiled report field 'verified_observations' must be a JSON array.")
        if not isinstance(open_questions, list):
            raise ReportCompileError("Compiled report field 'open_questions' must be a JSON array.")
        if not isinstance(contradictions, list):
            raise ReportCompileError("Compiled report field 'contradictions' must be a JSON array.")
        if not isinstance(decisions, list):
            raise ReportCompileError("Compiled report field 'decisions' must be a JSON array.")
        if not isinstance(suppression_records, list):
            raise ReportCompileError("Compiled report field 'suppression_records' must be a JSON array.")

        expected_counts = {
            "finding_count": len(findings),
            "verified_observation_count": len(verified_observations),
            "open_question_count": len(open_questions),
            "contradiction_count": len(contradictions),
            "decision_count": len(decisions),
        }
        for key, expected_value in expected_counts.items():
            actual_value = summary.get(key)
            if actual_value != expected_value:
                raise ReportCompileError(
                    f"Summary inconsistency for '{key}': expected {expected_value}, got {actual_value}."
                )

        # Coverage guardrail: if there are eligible verified observations but no findings,
        # suppression records must explain non-materialization.
        eligible_verified_ids: set[str] = set()
        for observation in verified_observations:
            if not isinstance(observation, dict):
                continue
            observation_id = observation.get("observation_id")
            evidence_class = observation.get("evidence_class")
            if (
                isinstance(observation_id, str)
                and observation_id
                and evidence_class in ALLOWED_FINDING_EVIDENCE_CLASSES
            ):
                eligible_verified_ids.add(observation_id)

        if eligible_verified_ids and not findings:
            suppressed_ids = {
                record.get("observation_id")
                for record in suppression_records
                if isinstance(record, dict) and isinstance(record.get("observation_id"), str)
            }
            missing_ids = sorted(eligible_verified_ids - suppressed_ids)
            if missing_ids:
                raise ReportCompileError(
                    "Eligible verified observations are not represented in findings and missing suppression "
                    f"coverage for: {', '.join(missing_ids)}"
                )

    @staticmethod
    def _derive_title_from_statement(statement: Any) -> str:
        if not isinstance(statement, str) or not statement.strip():
            return "Verified observation requires review"
        normalized = statement.strip()
        if len(normalized) <= 120:
            return normalized
        return normalized[:117] + "..."

    @staticmethod
    def _derive_confidence_from_observation(observation: dict[str, Any]) -> str:
        evidence_origin = observation.get("evidence_origin")
        if evidence_origin == "deterministic_pattern":
            return "high"
        if evidence_origin == "mixed_pattern_model":
            return "medium"
        return "medium"

    @staticmethod
    def _derive_severity_from_observation(observation: dict[str, Any]) -> str:
        statement = str(observation.get("statement", "")).lower()
        pattern_ids = observation.get("pattern_match_ids", [])
        pattern_text = " ".join(str(item).lower() for item in pattern_ids) if isinstance(pattern_ids, list) else ""
        signal = f"{statement} {pattern_text}"

        if "verify_signature" in signal or "jwt" in signal or "token" in signal:
            return "high"
        if "origins" in signal and "\"*\"" in signal:
            return "high"
        if "wildcard" in signal and "cors" in signal:
            return "high"
        return "medium"

    def _resolve_open_questions(
        self,
        issue_id: str,
        question_ids: Any,
        questions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if question_ids is None:
            return []
        if not isinstance(question_ids, list):
            raise ReportCompileError(f"Issue '{issue_id}' question_ids must be a JSON array.")

        open_questions: list[dict[str, Any]] = []
        for question_id in sorted(question_ids):
            question = questions.get(question_id)
            if question is None:
                raise ReportCompileError(
                    f"Issue '{issue_id}' references missing question '{question_id}'."
                )
            if question["status"] == "open":
                open_questions.append(_normalize(question))
        return open_questions

    def _resolve_contradictions(
        self,
        issue_id: str,
        contradiction_ids: Any,
        contradictions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if contradiction_ids is None:
            return []
        if not isinstance(contradiction_ids, list):
            raise ReportCompileError(f"Issue '{issue_id}' contradiction_ids must be a JSON array.")

        related: list[dict[str, Any]] = []
        for contradiction_id in sorted(contradiction_ids):
            contradiction = contradictions.get(contradiction_id)
            if contradiction is None:
                raise ReportCompileError(
                    f"Issue '{issue_id}' references missing contradiction '{contradiction_id}'."
                )
            related.append(_normalize(contradiction))
        return related

    def _build_verified_observation_entry(self, observation: dict[str, Any]) -> dict[str, Any]:
        provenance = observation.get("provenance", {})
        source_refs = provenance.get("source_refs", [])
        evidence_class = observation.get("evidence_class")
        if not isinstance(evidence_class, str) or not evidence_class:
            raise ReportCompileError(
                f"Verified observation '{observation.get('id', '<unknown>')}' must expose a non-empty "
                "evidence_class."
            )
        entry: dict[str, Any] = {
            "observation_id": observation["id"],
            "status": observation["status"],
            "statement": observation["statement"],
            "evidence_class": evidence_class,
            "source_refs": self._sorted_source_refs(source_refs),
        }
        evidence_origin = observation.get("evidence_origin")
        if evidence_origin:
            entry["evidence_origin"] = evidence_origin
        pattern_match_ids = observation.get("pattern_match_ids")
        if pattern_match_ids:
            entry["pattern_match_ids"] = pattern_match_ids
        return entry

    def _collect_candidates(
        self,
        candidates: dict[str, Any],
        audit_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Collect candidates for the current audit from the candidates namespace."""
        collected: dict[str, dict[str, Any]] = {}
        for candidate_id in sorted(candidates):
            candidate = candidates[candidate_id]
            if candidate.get("audit_id") != audit_id:
                continue
            collected[candidate_id] = {
                "candidate_id": candidate["id"],
                "candidate_type": candidate.get("candidate_type"),
                "status": candidate.get("status"),
                "proposed_claim": candidate.get("proposed_claim"),
                "confidence": candidate.get("confidence"),
                "promoted_observation_id": candidate.get("promoted_observation_id"),
            }
        return collected

    def _build_candidate_appendix(
        self,
        candidates: dict[str, Any],
        audit_id: str,
    ) -> dict[str, Any] | None:
        """
        Build the optional candidate appendix for v1.2 reports.

        IMPORTANT: This appendix is NON-AUTHORITATIVE and does NOT affect findings.
        It provides visibility into the candidate layer for debugging and analysis.
        """
        collected_candidates = self._collect_candidates(candidates, audit_id)
        if not collected_candidates:
            return None

        # Aggregate counts by type
        type_counts: dict[str, int] = {}
        for candidate_type in CANDIDATE_TYPES:
            type_counts[candidate_type] = 0
        for candidate in collected_candidates.values():
            candidate_type = candidate.get("candidate_type")
            if candidate_type in type_counts:
                type_counts[candidate_type] += 1

        # Aggregate counts by outcome
        outcome_counts: dict[str, int] = {}
        for outcome in CANDIDATE_OUTCOMES:
            outcome_counts[outcome] = 0
        for candidate in collected_candidates.values():
            status = candidate.get("status")
            if status in outcome_counts:
                outcome_counts[status] += 1

        # Build summary
        total_candidates = len(collected_candidates)
        promoted_count = outcome_counts.get("resolved_promoted", 0)
        rejected_count = outcome_counts.get("rejected", 0)
        routed_count = outcome_counts.get("routed_to_verify", 0)
        pending_count = outcome_counts.get("proposed", 0)

        return {
            "disclaimer": "NON-AUTHORITATIVE: Candidates are proposals that require verification. "
            "This appendix is for informational purposes only and does not affect findings.",
            "audit_id": audit_id,
            "summary": {
                "total_candidates": total_candidates,
                "counts_by_type": type_counts,
                "counts_by_outcome": outcome_counts,
                "promoted_count": promoted_count,
                "rejected_count": rejected_count,
                "routed_to_verify_count": routed_count,
                "pending_proposed_count": pending_count,
            },
            "candidates": list(collected_candidates.values()),
        }

    @staticmethod
    def _sorted_source_refs(source_refs: Any) -> list[dict[str, Any]]:
        if not isinstance(source_refs, list):
            return []
        normalized = [_normalize(source_ref) for source_ref in source_refs if isinstance(source_ref, dict)]
        return sorted(
            normalized,
            key=lambda item: (
                item.get("file_path", ""),
                item.get("line_range", {}).get("start", 0),
                item.get("line_range", {}).get("end", 0),
                item.get("snapshot_ref", ""),
                item.get("file_hash", ""),
            ),
        )

    @staticmethod
    def _sorted_entity_refs(entity_refs: Any) -> list[dict[str, Any]]:
        if not isinstance(entity_refs, list):
            return []
        normalized = [_normalize(entity_ref) for entity_ref in entity_refs if isinstance(entity_ref, dict)]
        return sorted(
            normalized,
            key=lambda item: (
                item.get("entity_type", ""),
                item.get("entity_id", ""),
            ),
        )
