from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


# =============================================================================
# Evidence Classes (STEP 0)
# =============================================================================

EVIDENCE_CLASSES = (
    "direct_code_fact",
    "derived_structural_fact",
    "inferred_hypothesis",
    "pattern_match",
    "blocked_verification",
)

EVIDENCE_ORIGINS = (
    "deterministic_pattern",
    "model_discovered",
    "mixed_pattern_model",
)
"""How the observation originated. Deterministic provenance of pattern scanner + LLM."""
ALLOWED_FINDING_EVIDENCE_CLASSES = frozenset(
    {
        "direct_code_fact",
        "derived_structural_fact",
        "pattern_match",
    }
)

# =============================================================================
# Schema v1.3 Constants (STEP 1)
# =============================================================================

EVIDENCE_SCHEMA_VERSION = "1.3.0"
"""Current evidence schema version."""

DEFAULT_LINE_START = 1
"""Default line_start when not provided."""

DEFAULT_LINE_END = 1
"""Default line_end when not provided."""


# =============================================================================
# Normalized Source Reference (STEP 1 - Schema v1.3)
# =============================================================================

@dataclass(frozen=True)
class NormalizedSourceRef:
    """
    Transport-valid source reference with explicit typing.

    Schema v1.3 fields:
    - file_path: str (required)
    - snapshot_ref: str (required)
    - line_range: dict with start, end (normalized)
    - range_inferred: bool (True if line_range was defaulted)
    - normalization_warning: str | None (explanation if inference occurred)
    - file_hash: str | None

    INVARIANTS:
    - line_range.start and line_range.end are ALWAYS valid integers
    - range_inferred=True means the original data was incomplete
    - normalization_warning explains WHY inference was needed
    """
    file_path: str
    """Path to the source file."""

    snapshot_ref: str
    """Snapshot reference for versioning."""

    line_range_start: int
    """Starting line number (1-indexed)."""

    line_range_end: int
    """Ending line number (inclusive)."""

    range_inferred: bool = False
    """True if line_range was defaulted due to missing/invalid data."""

    normalization_warning: str | None = None
    """Explanation of why normalization was needed."""

    file_hash: str | None = None
    """Optional hash of the file content."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for transport."""
        result: dict[str, Any] = {
            "file_path": self.file_path,
            "snapshot_ref": self.snapshot_ref,
            "line_range": {
                "start": self.line_range_start,
                "end": self.line_range_end,
            },
            "range_inferred": self.range_inferred,
        }
        if self.normalization_warning is not None:
            result["normalization_warning"] = self.normalization_warning
        if self.file_hash is not None:
            result["file_hash"] = self.file_hash
        return result

    @property
    def is_degraded(self) -> bool:
        """Check if this is a degraded/inferred evidence item."""
        return self.range_inferred


# =============================================================================
# Source Reference Normalization (STEP 1)
# =============================================================================

def normalize_source_ref(
    source_ref: dict[str, Any],
    *,
    default_line_start: int = DEFAULT_LINE_START,
    default_line_end: int = DEFAULT_LINE_END,
) -> NormalizedSourceRef | None:
    """
    Normalize a source reference to transport-valid form.

    This function implements STEP 1 invariant:
    > Every evidence item MUST be transport-valid and explicitly typed

    Missing or invalid line_range values are replaced with defaults
    and marked with range_inferred=True and normalization_warning.

    Args:
        source_ref: Raw source reference from worker output
        default_line_start: Default start line (default: 1)
        default_line_end: Default end line (default: 1)

    Returns:
        NormalizedSourceRef if file_path and snapshot_ref are valid, None otherwise

    Examples:
        >>> normalize_source_ref({
        ...     "file_path": "src/app.py",
        ...     "snapshot_ref": "abc123",
        ...     "line_range": {"start": 10, "end": 20}
        ... })
        NormalizedSourceRef(file_path='src/app.py', ..., range_inferred=False)

        >>> normalize_source_ref({
        ...     "file_path": "src/app.py",
        ...     "snapshot_ref": "abc123",
        ...     "line_range": {}
        ... })
        NormalizedSourceRef(file_path='src/app.py', ..., range_inferred=True,
                           normalization_warning='line_range.start missing or invalid')
    """
    # Validate required string fields
    file_path = source_ref.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    snapshot_ref = source_ref.get("snapshot_ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
        return None

    # Normalize file_path (forward slashes, stripped)
    file_path = file_path.replace("\\", "/").strip()
    snapshot_ref = snapshot_ref.strip()

    # Extract and normalize line_range
    line_range = source_ref.get("line_range")
    warnings: list[str] = []
    range_inferred = False

    if not isinstance(line_range, dict):
        # line_range is missing or not a dict - use defaults
        line_start = default_line_start
        line_end = default_line_end
        range_inferred = True
        warnings.append("line_range missing or not an object")
    else:
        # Extract start
        start = line_range.get("start")
        if not isinstance(start, int) or start < 1:
            line_start = default_line_start
            range_inferred = True
            warnings.append("line_range.start missing or invalid")
        else:
            line_start = start

        # Extract end
        end = line_range.get("end")
        if not isinstance(end, int) or end < 1:
            line_end = line_start  # Default to same as start
            if not range_inferred:  # Only add warning if start was valid
                range_inferred = True
                warnings.append("line_range.end missing or invalid")
        else:
            line_end = end

        # Validate start <= end
        if line_start > line_end:
            # This is a semantic error, but we normalize for transport
            line_end = line_start
            range_inferred = True
            warnings.append(f"line_range.start ({line_start}) > end; normalized end to {line_end}")

    # Build normalization warning
    normalization_warning = "; ".join(warnings) if warnings else None

    # Extract optional file_hash
    file_hash = source_ref.get("file_hash")
    if isinstance(file_hash, str) and file_hash.strip():
        file_hash = file_hash.strip().lower()
    else:
        file_hash = None

    return NormalizedSourceRef(
        file_path=file_path,
        snapshot_ref=snapshot_ref,
        line_range_start=line_start,
        line_range_end=line_end,
        range_inferred=range_inferred,
        normalization_warning=normalization_warning,
        file_hash=file_hash,
    )


def normalize_source_refs(
    source_refs: list[dict[str, Any]] | None,
    *,
    default_line_start: int = DEFAULT_LINE_START,
    default_line_end: int = DEFAULT_LINE_END,
) -> list[NormalizedSourceRef]:
    """
    Normalize a list of source references.

    Args:
        source_refs: List of raw source references
        default_line_start: Default start line
        default_line_end: Default end line

    Returns:
        List of normalized source references (invalid ones are filtered out)
    """
    if not isinstance(source_refs, list):
        return []

    normalized: list[NormalizedSourceRef] = []
    for source_ref in source_refs:
        if isinstance(source_ref, dict):
            normalized_ref = normalize_source_ref(
                source_ref,
                default_line_start=default_line_start,
                default_line_end=default_line_end,
            )
            if normalized_ref is not None:
                normalized.append(normalized_ref)

    return normalized


def normalize_evidence_class(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized in EVIDENCE_CLASSES:
        return normalized
    return None


def derive_observation_evidence_class(source_refs: Any) -> str:
    normalized_locations = {
        _source_ref_identity(source_ref)
        for source_ref in source_refs
        if isinstance(source_ref, dict) and _source_ref_identity(source_ref) is not None
    }
    if not normalized_locations:
        return "blocked_verification"
    if len(normalized_locations) == 1:
        return "direct_code_fact"
    return "derived_structural_fact"


def count_evidence_classes(
    observations: Iterable[dict[str, Any]],
    *,
    include_zero: bool = True,
) -> dict[str, int]:
    counts = {evidence_class: 0 for evidence_class in EVIDENCE_CLASSES}
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        evidence_class = normalize_evidence_class(observation.get("evidence_class"))
        if evidence_class is None:
            continue
        counts[evidence_class] += 1
    if include_zero:
        return counts
    return {
        evidence_class: count
        for evidence_class, count in counts.items()
        if count > 0
    }


def count_evidence_origins(
    observations: Iterable[dict[str, Any]],
    *,
    include_zero: bool = True,
) -> dict[str, int]:
    counts = {origin: 0 for origin in EVIDENCE_ORIGINS}
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        origin = observation.get("evidence_origin")
        if isinstance(origin, str) and origin in EVIDENCE_ORIGINS:
            counts[origin] += 1
    if include_zero:
        return counts
    return {
        origin: count
        for origin, count in counts.items()
        if count > 0
    }


def present_evidence_classes(observations: Iterable[dict[str, Any]]) -> list[str]:
    present = {
        evidence_class
        for observation in observations
        for evidence_class in [normalize_evidence_class(observation.get("evidence_class"))]
        if evidence_class is not None
    }
    return [evidence_class for evidence_class in EVIDENCE_CLASSES if evidence_class in present]


def _source_ref_identity(source_ref: dict[str, Any]) -> tuple[Any, ...] | None:
    file_path = source_ref.get("file_path")
    snapshot_ref = source_ref.get("snapshot_ref")
    line_range = source_ref.get("line_range")
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
        return None
    if not isinstance(line_range, dict):
        return None
    start = line_range.get("start")
    end = line_range.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    file_hash = source_ref.get("file_hash")
    return (
        file_path.replace("\\", "/").strip(),
        start,
        end,
        snapshot_ref.strip(),
        file_hash.strip().lower() if isinstance(file_hash, str) and file_hash.strip() else "",
    )
