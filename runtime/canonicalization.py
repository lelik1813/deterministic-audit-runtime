from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


class CanonicalizationError(Exception):
    """Raised when an event fails canonicalization validation."""
    pass


CANONICALIZATION_SCHEMA_VERSION = "1.0.0"
CANONICAL_EVENT_TYPES = {
    "observation.proposed",
    "observation.verified",
    "observation.rejected",
    "hypothesis.proposed",
    "hypothesis.sent_to_verification",
    "hypothesis.supported",
    "hypothesis.rejected",
    "question.opened",
    "issue.proposed",
    "candidate.proposed",
}
ENTITY_PREFIX_BY_EVENT_TYPE = {
    "observation.proposed": "obs",
    "hypothesis.proposed": "hyp",
    "question.opened": "question",
    "issue.proposed": "issue",
    "candidate.proposed": "candidate",
}
VALID_CANDIDATE_TYPES = frozenset({
    "risk_candidate",
    "policy_candidate",
    "cross_file_correlation",
    "verification_target",
})
TEXT_FIELD_BY_EVENT_TYPE = {
    "observation.proposed": "statement",
    "observation.verified": "statement",
    "observation.rejected": "statement",
    "hypothesis.proposed": "statement",
    "question.opened": "prompt",
    "issue.proposed": "title",
    "candidate.proposed": "proposed_claim",
}
GENERIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "before",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "via",
    "when",
    "with",
}
FIELD_STOPWORDS = {
    "observation": {"function", "parameter", "argument"},
    "hypothesis": {"function", "parameter", "argument"},
    "question": {"function", "parameter", "argument"},
    "issue_title": {"function", "user", "caller"},
    "candidate_claim": {"function", "parameter", "argument", "code", "file"},
}
IDENTIFIER_TOKEN_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_\.]*\b")
ASSIGNMENT_TOKEN_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(true|false|null|none|\d+)\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[a-z0-9]+")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def semantic_digest(value: Any, *, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()[:length]


def canonicalize_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(event))
    _normalize_payload_display_fields(normalized)
    _normalize_payload_arrays(normalized)

    event_type = normalized.get("event_type")
    if event_type not in CANONICAL_EVENT_TYPES:
        return normalized

    # v1.3 Step 4: Validate no-single-fact shortcut for supported hypotheses
    _validate_supported_hypothesis_evidence(normalized)

    entity_id = _canonical_entity_id(normalized)
    if entity_id is not None:
        normalized["entity_id"] = entity_id
        payload = normalized.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            payload["id"] = entity_id

    event_fingerprint = build_event_fingerprint(normalized)
    event_type_token = str(event_type).replace(".", "_")
    event_digest = semantic_digest(event_fingerprint)
    normalized["id"] = f"event_{event_type_token}_{event_digest}"
    normalized["idempotency_key"] = f"semantic:{event_type_token}:{event_digest}"
    return normalized


def build_event_fingerprint(event: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(event))
    _normalize_payload_display_fields(normalized)
    _normalize_payload_arrays(normalized)

    event_type = normalized.get("event_type")
    payload = normalized.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    base = {
        "schema_version": CANONICALIZATION_SCHEMA_VERSION,
        "audit_id": normalized.get("audit_id"),
        "entity_type": normalized.get("entity_type"),
        "event_type": event_type,
        "snapshot_ref": normalized.get("snapshot_ref"),
    }

    if event_type == "observation.proposed":
        base["semantic_payload"] = {
            "claim": canonical_text_identity(payload.get("statement"), field_kind="observation"),
            "evidence_class": payload.get("evidence_class"),
            "source_refs": canonical_source_ref_identities(
                payload.get("provenance", {}).get("source_refs")
            ),
        }
        return base

    if event_type in {"observation.verified", "observation.rejected"}:
        base["semantic_payload"] = {
            "observation_id": normalized.get("entity_id"),
            "status": payload.get("status"),
            "claim": canonical_text_identity(payload.get("statement"), field_kind="observation"),
            "evidence_class": payload.get("evidence_class"),
            "source_refs": canonical_source_ref_identities(
                payload.get("provenance", {}).get("source_refs")
            ),
        }
        return base

    if event_type == "hypothesis.proposed":
        base["semantic_payload"] = {
            "claim": canonical_text_identity(payload.get("statement"), field_kind="hypothesis"),
            "supporting_source_refs": canonical_source_ref_identities(
                payload.get("supporting_source_refs")
            ),
            "supporting_hypothesis_ids": normalize_string_list(
                payload.get("supporting_hypothesis_ids")
            ),
            "contradicting_hypothesis_ids": normalize_string_list(
                payload.get("contradicting_hypothesis_ids")
            ),
        }
        return base

    if event_type == "question.opened":
        base["semantic_payload"] = {
            "question": canonical_text_identity(payload.get("prompt"), field_kind="question"),
            "related_entity_refs": canonical_related_entity_refs(
                payload.get("related_entity_refs")
            ),
        }
        context = payload.get("context")
        if isinstance(context, str) and context.strip():
            base["semantic_payload"]["context"] = canonical_text_identity(
                context,
                field_kind="question",
            )
        return base

    if event_type == "issue.proposed":
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        base["semantic_payload"] = {
            "title": canonical_text_identity(payload.get("title"), field_kind="issue_title"),
            "evidence": canonical_issue_evidence(evidence),
            "severity": payload.get("severity"),
            "severity_rule_ref": payload.get("severity_rule_ref"),
        }
        return base

    if event_type == "candidate.proposed":
        candidate_fingerprint = canonical_candidate_fingerprint(normalized, payload)
        if candidate_fingerprint is not None:
            base["semantic_payload"] = candidate_fingerprint
            return base
        # Unknown candidate type - use fallback with explicit marker
        base["semantic_payload"] = {
            "candidate_type": payload.get("candidate_type", "unknown"),
            "raw_payload": build_fallback_payload_fingerprint({"payload": payload}),
        }
        return base

    base["semantic_payload"] = build_fallback_payload_fingerprint(normalized)
    return base


def semantic_equivalent(event_a: dict[str, Any], event_b: dict[str, Any]) -> bool:
    fingerprint_a = build_event_fingerprint(event_a)
    fingerprint_b = build_event_fingerprint(event_b)
    return fingerprint_a == fingerprint_b


def build_fallback_payload_fingerprint(event: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(event))
    for field_name in (
        "id",
        "idempotency_key",
        "occurred_at",
        "actor",
        "acceptance",
        "caused_by_event_id",
    ):
        normalized.pop(field_name, None)
    return normalized


def canonical_text_identity(value: Any, *, field_kind: str) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"tokens": []}

    display_text = normalize_display_text(value)
    normalized_text = _normalize_text_for_tokens(display_text)
    code_tokens = _extract_code_tokens(normalized_text)
    field_stopwords = GENERIC_STOPWORDS | FIELD_STOPWORDS.get(field_kind, set())
    word_tokens = sorted(
        {
            stemmed
            for raw_token in WORD_RE.findall(normalized_text)
            for stemmed in [_stem_token(raw_token)]
            if len(stemmed) >= 3
            and stemmed not in field_stopwords
            and stemmed not in code_tokens
        }
    )
    tokens = sorted(set(code_tokens) | set(word_tokens))
    if tokens:
        return {"tokens": tokens}
    return {"normalized_text": normalized_text}


def normalize_display_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())


def normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized_items = {
        normalize_display_text(item)
        for item in values
        if isinstance(item, str) and normalize_display_text(item)
    }
    return sorted(normalized_items)


def normalize_path(value: str) -> str:
    return value.replace("\\", "/").strip()


def canonical_source_ref_identities(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    normalized_refs: list[dict[str, Any]] = []
    for source_ref in value:
        if not isinstance(source_ref, dict):
            continue
        line_range = source_ref.get("line_range")
        if not isinstance(line_range, dict):
            continue
        start = line_range.get("start")
        end = line_range.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue

        normalized_ref: dict[str, Any] = {
            "file_path": normalize_path(str(source_ref.get("file_path", ""))),
            "line_range": {"start": start, "end": end},
            "snapshot_ref": source_ref.get("snapshot_ref"),
        }
        file_hash = source_ref.get("file_hash")
        if isinstance(file_hash, str) and file_hash:
            normalized_ref["file_hash"] = file_hash.lower()
        normalized_refs.append(normalized_ref)

    return sorted(
        normalized_refs,
        key=lambda item: (
            item.get("file_path"),
            item["line_range"]["start"],
            item["line_range"]["end"],
            item.get("snapshot_ref"),
            item.get("file_hash", ""),
        ),
    )


def canonical_related_entity_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    normalized_refs = [
        {
            "entity_type": entity_ref["entity_type"],
            "entity_id": entity_ref["entity_id"],
        }
        for entity_ref in value
        if isinstance(entity_ref, dict)
        and isinstance(entity_ref.get("entity_type"), str)
        and isinstance(entity_ref.get("entity_id"), str)
    ]
    return sorted(
        normalized_refs,
        key=lambda item: (item["entity_type"], item["entity_id"]),
    )


def canonical_issue_evidence(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {"observation_ids": []}

    evidence = {
        "observation_ids": normalize_string_list(value.get("observation_ids")),
    }
    question_ids = normalize_string_list(value.get("question_ids"))
    contradiction_ids = normalize_string_list(value.get("contradiction_ids"))
    if question_ids:
        evidence["question_ids"] = question_ids
    if contradiction_ids:
        evidence["contradiction_ids"] = contradiction_ids
    return evidence


# ============================================================================
# Candidate Canonicalization Functions (v1.2 Step 9)
# ============================================================================

def canonical_candidate_evidence_refs(value: Any) -> list[dict[str, Any]]:
    """Normalize candidate evidence references for semantic identity.

    Candidates use supporting_evidence_refs which may include both
    source_refs and observation_ids. This function normalizes them
    for consistent comparison.
    """
    if not isinstance(value, list):
        return []

    normalized_refs: list[dict[str, Any]] = []
    for ref in value:
        if not isinstance(ref, dict):
            continue

        # Handle source_ref style references
        if "file_path" in ref:
            line_range = ref.get("line_range")
            if isinstance(line_range, dict):
                start = line_range.get("start")
                end = line_range.get("end")
                if isinstance(start, int) and isinstance(end, int):
                    normalized_ref: dict[str, Any] = {
                        "file_path": normalize_path(str(ref.get("file_path", ""))),
                        "line_range": {"start": start, "end": end},
                    }
                    snapshot_ref = ref.get("snapshot_ref")
                    if isinstance(snapshot_ref, str):
                        normalized_ref["snapshot_ref"] = snapshot_ref
                    normalized_refs.append(normalized_ref)
                    continue

        # Handle observation_id style references
        if "observation_id" in ref:
            obs_id = ref.get("observation_id")
            if isinstance(obs_id, str):
                normalized_refs.append({"observation_id": obs_id})
                continue

        # Handle simple string references (observation IDs)
        if isinstance(ref, str):
            normalized_refs.append({"observation_id": ref})
            continue

    return sorted(
        normalized_refs,
        key=lambda item: canonical_json(item),
    )


def canonical_observation_id_refs(value: Any) -> list[str]:
    """Normalize observation ID lists for semantic identity.

    Used for trigger_observation_ids and related_observation_ids.
    """
    return normalize_string_list(value)


def canonical_file_paths(value: Any) -> list[str]:
    """Normalize file path lists for semantic identity.

    Used for involved_file_paths in cross_file_correlation.
    Paths are normalized to forward slashes and sorted.
    """
    if not isinstance(value, list):
        return []

    normalized_paths = {
        normalize_path(str(path))
        for path in value
        if isinstance(path, (str, int, float)) and str(path).strip()
    }
    return sorted(normalized_paths)


def canonical_verification_target(value: Any) -> dict[str, Any]:
    """Normalize verification target for semantic identity.

    Used for verification_target candidates.
    """
    if not isinstance(value, dict):
        return {}

    target_type = value.get("target_type")
    target_id = value.get("target_id")

    if not isinstance(target_type, str) or not isinstance(target_id, str):
        return {}

    normalized: dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id,
    }

    # Include verification_questions if present (affects semantic identity)
    questions = value.get("verification_questions")
    if isinstance(questions, list):
        normalized["verification_questions"] = normalize_string_list(questions)

    return normalized


def canonical_risk_candidate_fingerprint(
    audit_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build semantic fingerprint for risk_candidate.

    Identity is based on:
    - audit_id
    - proposed_claim (normalized text)
    - risk_category
    - supporting_evidence_refs (normalized)
    - trigger_observation_ids (normalized)

    Conservative: candidates with different evidence scope remain distinct.
    """
    return {
        "candidate_type": "risk_candidate",
        "audit_id": audit_id,
        "proposed_claim": canonical_text_identity(
            payload.get("proposed_claim"), field_kind="candidate_claim"
        ),
        "risk_category": payload.get("risk_category"),
        "supporting_evidence_refs": canonical_candidate_evidence_refs(
            payload.get("supporting_evidence_refs")
        ),
        "trigger_observation_ids": canonical_observation_id_refs(
            payload.get("trigger_observation_ids")
        ),
    }


def canonical_policy_candidate_fingerprint(
    audit_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build semantic fingerprint for policy_candidate.

    Identity is based on:
    - audit_id
    - proposed_claim (normalized text)
    - policy_rule_ref (required)
    - supporting_evidence_refs (normalized)
    - trigger_observation_ids (normalized)

    Conservative: candidates with different policy_rule_ref remain distinct.
    """
    return {
        "candidate_type": "policy_candidate",
        "audit_id": audit_id,
        "proposed_claim": canonical_text_identity(
            payload.get("proposed_claim"), field_kind="candidate_claim"
        ),
        "policy_rule_ref": payload.get("policy_rule_ref"),
        "supporting_evidence_refs": canonical_candidate_evidence_refs(
            payload.get("supporting_evidence_refs")
        ),
        "trigger_observation_ids": canonical_observation_id_refs(
            payload.get("trigger_observation_ids")
        ),
    }


def canonical_cross_file_correlation_fingerprint(
    audit_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build semantic fingerprint for cross_file_correlation.

    Identity is based on:
    - audit_id
    - proposed_claim (normalized text)
    - relationship_type
    - involved_file_paths (normalized, sorted)
    - related_observation_ids (normalized)

    Conservative: candidates with different file paths remain distinct.
    """
    return {
        "candidate_type": "cross_file_correlation",
        "audit_id": audit_id,
        "proposed_claim": canonical_text_identity(
            payload.get("proposed_claim"), field_kind="candidate_claim"
        ),
        "relationship_type": payload.get("relationship_type"),
        "involved_file_paths": canonical_file_paths(
            payload.get("involved_file_paths")
        ),
        "related_observation_ids": canonical_observation_id_refs(
            payload.get("related_observation_ids")
        ),
    }


def canonical_verification_target_fingerprint(
    audit_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build semantic fingerprint for verification_target.

    Identity is based on:
    - audit_id
    - verification_target (normalized)
    - supporting_evidence_refs (normalized)
    - trigger_observation_ids (normalized)

    Conservative: candidates with different targets remain distinct.
    """
    return {
        "candidate_type": "verification_target",
        "audit_id": audit_id,
        "verification_target": canonical_verification_target(
            payload.get("verification_target")
        ),
        "supporting_evidence_refs": canonical_candidate_evidence_refs(
            payload.get("supporting_evidence_refs")
        ),
        "trigger_observation_ids": canonical_observation_id_refs(
            payload.get("trigger_observation_ids")
        ),
    }


def canonical_candidate_fingerprint(
    event: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Build semantic fingerprint for any candidate type.

    Dispatches to type-specific canonicalization based on candidate_type.
    Returns None if candidate_type is unknown or missing.

    Conservative approach:
    - Different candidate types NEVER merge
    - Different evidence scope preserves distinction
    - Missing required fields prevent canonicalization
    """
    candidate_type = payload.get("candidate_type")
    audit_id = event.get("audit_id")

    if candidate_type == "risk_candidate":
        return canonical_risk_candidate_fingerprint(audit_id, payload)

    if candidate_type == "policy_candidate":
        return canonical_policy_candidate_fingerprint(audit_id, payload)

    if candidate_type == "cross_file_correlation":
        return canonical_cross_file_correlation_fingerprint(audit_id, payload)

    if candidate_type == "verification_target":
        return canonical_verification_target_fingerprint(audit_id, payload)

    return None


def _canonical_entity_id(event: dict[str, Any]) -> str | None:
    event_type = event.get("event_type")
    prefix = ENTITY_PREFIX_BY_EVENT_TYPE.get(event_type)
    if prefix is None:
        return None

    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None

    if event_type == "observation.proposed":
        fingerprint = {
            "audit_id": event.get("audit_id"),
            "entity_type": "observation",
            "snapshot_ref": event.get("snapshot_ref"),
            "claim": canonical_text_identity(payload.get("statement"), field_kind="observation"),
            "evidence_class": payload.get("evidence_class"),
            "source_refs": canonical_source_ref_identities(
                payload.get("provenance", {}).get("source_refs")
            ),
        }
        return f"{prefix}_{semantic_digest(fingerprint)}"

    if event_type == "hypothesis.proposed":
        fingerprint = {
            "audit_id": event.get("audit_id"),
            "entity_type": "hypothesis",
            "snapshot_ref": event.get("snapshot_ref"),
            "claim": canonical_text_identity(payload.get("statement"), field_kind="hypothesis"),
            "supporting_source_refs": canonical_source_ref_identities(
                payload.get("supporting_source_refs")
            ),
            "supporting_hypothesis_ids": normalize_string_list(
                payload.get("supporting_hypothesis_ids")
            ),
            "contradicting_hypothesis_ids": normalize_string_list(
                payload.get("contradicting_hypothesis_ids")
            ),
        }
        return f"{prefix}_{semantic_digest(fingerprint)}"

    if event_type == "question.opened":
        fingerprint = {
            "audit_id": event.get("audit_id"),
            "entity_type": "question",
            "snapshot_ref": event.get("snapshot_ref"),
            "question": canonical_text_identity(payload.get("prompt"), field_kind="question"),
            "related_entity_refs": canonical_related_entity_refs(
                payload.get("related_entity_refs")
            ),
        }
        context = payload.get("context")
        if isinstance(context, str) and context.strip():
            fingerprint["context"] = canonical_text_identity(context, field_kind="question")
        return f"{prefix}_{semantic_digest(fingerprint)}"

    if event_type == "issue.proposed":
        fingerprint = {
            "audit_id": event.get("audit_id"),
            "entity_type": "issue",
            "snapshot_ref": event.get("snapshot_ref"),
            "title": canonical_text_identity(payload.get("title"), field_kind="issue_title"),
            "evidence": canonical_issue_evidence(payload.get("evidence")),
            "severity": payload.get("severity"),
            "severity_rule_ref": payload.get("severity_rule_ref"),
        }
        return f"{prefix}_{semantic_digest(fingerprint)}"

    if event_type == "candidate.proposed":
        candidate_fingerprint = canonical_candidate_fingerprint(event, payload)
        if candidate_fingerprint is not None:
            fingerprint = {
                "entity_type": "candidate",
                **candidate_fingerprint,
            }
            return f"{prefix}_{semantic_digest(fingerprint)}"
        return None

    return None


def _normalize_payload_display_fields(event: dict[str, Any]) -> None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return

    event_type = event.get("event_type")
    text_field = TEXT_FIELD_BY_EVENT_TYPE.get(event_type)
    if text_field is not None and isinstance(payload.get(text_field), str):
        payload[text_field] = normalize_display_text(payload[text_field])

    if event_type == "issue.proposed" and isinstance(payload.get("summary"), str):
        payload["summary"] = normalize_display_text(payload["summary"])

    if event_type == "question.opened" and isinstance(payload.get("context"), str):
        payload["context"] = normalize_display_text(payload["context"])

    if event_type == "hypothesis.proposed" and isinstance(payload.get("rationale"), str):
        payload["rationale"] = normalize_display_text(payload["rationale"])

    if event_type == "candidate.proposed":
        if isinstance(payload.get("proposed_claim"), str):
            payload["proposed_claim"] = normalize_display_text(payload["proposed_claim"])
        if isinstance(payload.get("reasoning_basis"), str):
            payload["reasoning_basis"] = normalize_display_text(payload["reasoning_basis"])


def _normalize_payload_arrays(event: dict[str, Any]) -> None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return

    if event.get("entity_type") == "observation":
        provenance = payload.get("provenance")
        if isinstance(provenance, dict):
            source_refs = provenance.get("source_refs")
            provenance["source_refs"] = _normalize_source_refs_for_storage(source_refs)
        return

    if event.get("entity_type") == "hypothesis":
        payload["supporting_source_refs"] = _normalize_source_refs_for_storage(
            payload.get("supporting_source_refs")
        )
        # v1.3 Step 3: Normalize hypothesis relationship arrays
        payload["supporting_hypothesis_ids"] = normalize_string_list(
            payload.get("supporting_hypothesis_ids")
        )
        payload["contradicting_hypothesis_ids"] = normalize_string_list(
            payload.get("contradicting_hypothesis_ids")
        )
        # v1.3 Step 4: Normalize verification_basis arrays
        verification_basis = payload.get("verification_basis")
        if isinstance(verification_basis, dict):
            verification_basis["supporting_observations"] = normalize_string_list(
                verification_basis.get("supporting_observations")
            )
            verification_basis["supporting_hypotheses"] = normalize_string_list(
                verification_basis.get("supporting_hypotheses")
            )
            verification_basis["missing_evidence"] = normalize_string_list(
                verification_basis.get("missing_evidence")
            )
            # Normalize contradictions_detected
            contradictions = verification_basis.get("contradictions_detected", [])
            if isinstance(contradictions, list):
                normalized_contradictions = []
                for c in contradictions:
                    if isinstance(c, dict) and "contradicting_hypothesis_id" in c:
                        normalized_contradictions.append({
                            "contradicting_hypothesis_id": c["contradicting_hypothesis_id"],
                            "description": normalize_display_text(c.get("description", "")),
                        })
                verification_basis["contradictions_detected"] = sorted(
                    normalized_contradictions,
                    key=lambda x: x["contradicting_hypothesis_id"]
                )
        return

    if event.get("entity_type") == "question":
        payload["related_entity_refs"] = canonical_related_entity_refs(
            payload.get("related_entity_refs")
        )
        return

    if event.get("entity_type") == "issue":
        evidence = payload.get("evidence")
        if isinstance(evidence, dict):
            evidence["observation_ids"] = normalize_string_list(evidence.get("observation_ids"))
            if "question_ids" in evidence:
                evidence["question_ids"] = normalize_string_list(evidence.get("question_ids"))
            if "contradiction_ids" in evidence:
                evidence["contradiction_ids"] = normalize_string_list(
                    evidence.get("contradiction_ids")
                )
        return

    if event.get("entity_type") == "contradiction":
        payload["conflicting_entity_refs"] = canonical_related_entity_refs(
            payload.get("conflicting_entity_refs")
        )
        payload["source_refs"] = _normalize_source_refs_for_storage(payload.get("source_refs"))
        return

    if event.get("entity_type") == "decision":
        payload["source_refs"] = _normalize_source_refs_for_storage(payload.get("source_refs"))
        return

    if event.get("entity_type") == "candidate":
        # Normalize supporting_evidence_refs
        payload["supporting_evidence_refs"] = canonical_candidate_evidence_refs(
            payload.get("supporting_evidence_refs")
        )
        # Normalize trigger_observation_ids / related_observation_ids
        if "trigger_observation_ids" in payload:
            payload["trigger_observation_ids"] = canonical_observation_id_refs(
                payload.get("trigger_observation_ids")
            )
        if "related_observation_ids" in payload:
            payload["related_observation_ids"] = canonical_observation_id_refs(
                payload.get("related_observation_ids")
            )
        # Normalize involved_file_paths for cross_file_correlation
        if "involved_file_paths" in payload:
            payload["involved_file_paths"] = canonical_file_paths(
                payload.get("involved_file_paths")
            )
        return


def _normalize_source_refs_for_storage(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    normalized_refs: list[dict[str, Any]] = []
    for source_ref in canonical_source_ref_identities(value):
        storage_ref = json.loads(json.dumps(source_ref))
        excerpt = None
        for candidate in value:
            if not isinstance(candidate, dict):
                continue
            line_range = candidate.get("line_range")
            if not isinstance(line_range, dict):
                continue
            if (
                normalize_path(str(candidate.get("file_path", ""))) == storage_ref["file_path"]
                and line_range.get("start") == storage_ref["line_range"]["start"]
                and line_range.get("end") == storage_ref["line_range"]["end"]
                and candidate.get("snapshot_ref") == storage_ref["snapshot_ref"]
            ):
                excerpt = candidate.get("excerpt")
                break
        if isinstance(excerpt, str) and excerpt:
            storage_ref["excerpt"] = normalize_display_text(excerpt)
        normalized_refs.append(storage_ref)
    return normalized_refs


def _normalize_text_for_tokens(value: str) -> str:
    normalized = normalize_display_text(value).casefold()
    normalized = normalized.replace("`", " ")
    normalized = normalized.replace("'", " ")
    normalized = normalized.replace('"', " ")
    normalized = normalized.replace("/", " ")
    normalized = normalized.replace("(", " ")
    normalized = normalized.replace(")", " ")
    normalized = normalized.replace(",", " ")
    normalized = normalized.replace(":", " ")
    normalized = normalized.replace(";", " ")
    normalized = normalized.replace("?", " ")
    normalized = normalized.replace("!", " ")
    normalized = normalized.replace("-", " ")
    return " ".join(normalized.split())


def _extract_code_tokens(normalized_text: str) -> list[str]:
    code_tokens = {
        match.group(0).lower()
        for match in IDENTIFIER_TOKEN_RE.finditer(normalized_text)
        if "." in match.group(0) or "_" in match.group(0)
    }
    for assignment in ASSIGNMENT_TOKEN_RE.finditer(normalized_text):
        code_tokens.add(f"{assignment.group(1).lower()}={assignment.group(2).lower()}")
    return sorted(code_tokens)


def _stem_token(token: str) -> str:
    for suffix in ("ing", "edly", "edly", "ed", "es", "s", "ly"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _validate_supported_hypothesis_evidence(event: dict[str, Any]) -> None:
    """Validate that supported hypotheses have supporting observations.

    v1.3 Step 4: Enforces the "no single-fact shortcut" rule.
    A hypothesis cannot be marked as "supported" without at least one
    supporting observation in its verification_basis.

    Raises:
        CanonicalizationError: If hypothesis.supported event has empty
            supporting_observations in verification_basis.
    """
    event_type = event.get("event_type")
    if event_type != "hypothesis.supported":
        return

    payload = event.get("payload")
    if not isinstance(payload, dict):
        return

    verification_basis = payload.get("verification_basis")
    if not isinstance(verification_basis, dict):
        raise CanonicalizationError(
            f"Supported hypothesis must have verification_basis. "
            f"Event: {event.get('id', 'unknown')}"
        )

    supporting_observations = verification_basis.get("supporting_observations")
    if not isinstance(supporting_observations, list) or len(supporting_observations) == 0:
        raise CanonicalizationError(
            f"Supported hypothesis must have at least one supporting observation "
            f"(no single-fact shortcut). Event: {event.get('id', 'unknown')}"
        )
