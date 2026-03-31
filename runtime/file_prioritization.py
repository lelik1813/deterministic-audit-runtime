"""
File Prioritization (STEP 6)

This module implements deterministic, explainable, yield-oriented file selection.

Core Invariant:
File selection MUST be deterministic, explainable, and yield-oriented

Scope:
Scoring function with factors:
- entrypoints: Is this an entry point (main, app, server, index)?
- auth/config proximity: How close to auth/config surfaces?
- execution surface: Is this part of the execution surface (routes, handlers)?
- dependency relevance: How many files depend on this? How many does it depend on?
- churn: How frequently has this file changed?
- exposure: Is this file exposed to external input (API, network)?

Out of scope:
- ML-based prioritization
- Adaptive runtime learning

Artifacts:
- priority_score(file)
- scoring explanation
- deterministic ordering

DoD:
- no more first-N selection
- same snapshot → same ordering
- explainable selection
- measurable yield improvement
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any


# =============================================================================
# Scoring Constants
# =============================================================================

# Weight factors for each scoring component (must sum to 1.0 for normalized scores)
FACTOR_WEIGHTS = {
    "entrypoint": 0.20,
    "auth_config_proximity": 0.25,
    "execution_surface": 0.20,
    "dependency_relevance": 0.15,
    "churn": 0.10,
    "exposure": 0.10,
}

# Maximum score for each factor (0-10 scale)
MAX_FACTOR_SCORE = 10.0

# Entrypoint patterns (files that are application entry points)
ENTRYPOINT_PATTERNS = frozenset({
    "main.py", "app.py", "server.py", "wsgi.py", "asgi.py",
    "index.js", "index.ts", "server.js", "server.ts", "app.js", "app.ts",
    "main.go", "main.rs", "main.java",
    "__init__.py",  # Package entry points
})

# Entrypoint directory patterns (specific directories, not generic like 'src')
ENTRYPOINT_DIR_PATTERNS = frozenset({
    "cmd", "main", "server",  # Go-style cmd/, main/
    "bin", "scripts",  # Executable scripts
})

# Auth/config surface patterns (from coverage/surface.py)
AUTH_PATTERNS = frozenset({
    "auth", "login", "session", "token", "password", "permission",
    "authenticate", "access_control", "login_required", "oauth", "jwt",
})

CONFIG_PATTERNS = frozenset({
    "config", "setting", "debug", "environment", "env", "cors", "tls", "ssl",
    "secret", "credential", "key",
})

# Execution surface patterns
EXECUTION_SURFACE_PATTERNS = frozenset({
    "route", "endpoint", "handler", "controller", "api", "rest", "graphql",
    "view", "middleware", "service", "worker", "task", "job",
})

# Exposure patterns (files that handle external input)
EXPOSURE_PATTERNS = frozenset({
    "api", "route", "endpoint", "handler", "controller", "view",
    "request", "response", "input", "form", "upload", "download",
    "websocket", "socket", "http", "rest", "graphql",
})

# High-churn file patterns (files that change frequently)
HIGH_CHURN_PATTERNS = frozenset({
    "test", "spec", "mock", "fixture",
})


# =============================================================================
# Scoring Dataclasses
# =============================================================================

@dataclass(frozen=True)
class FactorScore:
    """Score for a single prioritization factor."""
    factor_name: str
    score: float  # 0-10 scale
    weight: float  # Weight in overall score
    reasoning: str  # Explanation of why this score was assigned
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= MAX_FACTOR_SCORE:
            raise ValueError(f"Score must be in [0, {MAX_FACTOR_SCORE}], got {self.score}")


@dataclass(frozen=True)
class FileScore:
    """Complete prioritization score for a file with breakdown."""
    file_path: str
    total_score: float  # Weighted total score (0-10 scale)
    factor_scores: dict[str, FactorScore]
    ranking_tier: str  # "high", "medium", "low"
    priority_reason: str  # Primary reason for the priority level

    @property
    def is_high_priority(self) -> bool:
        return self.total_score >= 7.0

    @property
    def is_medium_priority(self) -> bool:
        return 4.0 <= self.total_score < 7.0

    @property
    def is_low_priority(self) -> bool:
        return self.total_score < 4.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "total_score": round(self.total_score, 3),
            "factor_scores": {
                name: {
                    "score": round(fs.score, 3),
                    "weight": fs.weight,
                    "reasoning": fs.reasoning,
                }
                for name, fs in self.factor_scores.items()
            },
            "ranking_tier": self.ranking_tier,
            "priority_reason": self.priority_reason,
        }


@dataclass(frozen=True)
class FilePrioritizationContext:
    """Context needed for file prioritization scoring.

    This context is derived from the snapshot and provides the data
    needed to compute file scores deterministically.
    """
    snapshot_ref: str
    """Reference to the snapshot for deterministic ordering."""

    files: frozenset[str]
    """Set of all files in the snapshot."""

    auth_proximity_map: dict[str, int]
    """Map of file path → distance to nearest auth surface (0 = direct, -1 = not reachable)."""

    config_proximity_map: dict[str, int]
    """Map of file path → distance to nearest config surface (0 = direct, -1 = not reachable)."""

    dependency_indegree: dict[str, int]
    """Map of file path → number of files that depend on this file."""

    dependency_outdegree: dict[str, int]
    """Map of file path → number of files this file depends on."""

    churn_scores: dict[str, float]
    """Map of file path → churn score (0-10 scale, based on git history or patterns)."""

    surface_category_map: dict[str, str]
    """Map of file path → inferred surface category."""

    def get_deterministic_sort_key(self, file_path: str) -> str:
        """Generate a deterministic sort key for tie-breaking.

        Uses SHA256 hash of snapshot_ref + file_path to ensure
        same snapshot always produces same ordering.
        """
        content = f"{self.snapshot_ref}:{file_path}"
        return hashlib.sha256(content.encode()).hexdigest()


# =============================================================================
# Scoring Functions
# =============================================================================

def _score_entrypoint(file_path: str, context: FilePrioritizationContext) -> FactorScore:
    """Score file based on whether it's an entry point.

    Entrypoints are files that serve as application entry points:
    - main.py, app.py, server.py
    - Files in cmd/, main/, server/ directories (direct child)
    - __init__.py for packages
    """
    path = PurePosixPath(file_path)
    filename = path.name.lower()
    parent_dir = str(path.parent).lower() if path.parent != PurePosixPath(".") else ""

    score = 0.0
    reasoning_parts = []

    # Check filename patterns
    if filename in ENTRYPOINT_PATTERNS:
        score = 10.0
        reasoning_parts.append(f"Filename '{filename}' is a known entry point pattern")

    # Check directory patterns - must be exact match on path segment, not substring
    elif parent_dir:
        # Split path into segments and check each
        path_segments = parent_dir.split("/")
        if any(segment in ENTRYPOINT_DIR_PATTERNS for segment in path_segments):
            score = 8.0
            reasoning_parts.append(f"Directory '{parent_dir}' suggests entry point")

    # Check if root-level important file
    elif parent_dir == "" and filename in {"index.js", "index.ts", "main.py", "app.py"}:
        score = 10.0
        reasoning_parts.append("Root-level entry point file")

    # Default score
    if not reasoning_parts:
        reasoning_parts.append("Not identified as an entry point")
        score = 0.0

    return FactorScore(
        factor_name="entrypoint",
        score=score,
        weight=FACTOR_WEIGHTS["entrypoint"],
        reasoning="; ".join(reasoning_parts),
        evidence={"filename": filename, "parent_dir": parent_dir},
    )


def _score_auth_config_proximity(
    file_path: str,
    context: FilePrioritizationContext,
) -> FactorScore:
    """Score file based on proximity to auth/config surfaces.

    Files closer to auth/config surfaces are higher priority because:
    - They may affect security posture
    - Changes may impact access control
    - Configuration drift may cause issues
    """
    auth_distance = context.auth_proximity_map.get(file_path, -1)
    config_distance = context.config_proximity_map.get(file_path, -1)

    # Use the minimum distance (closer to either auth OR config)
    min_distance = min(
        auth_distance if auth_distance >= 0 else float('inf'),
        config_distance if config_distance >= 0 else float('inf'),
    )

    if min_distance == float('inf'):
        score = 0.0
        reasoning = "No proximity to auth or config surfaces"
    elif min_distance == 0:
        score = 10.0
        reasoning = "Directly part of auth or config surface"
    elif min_distance == 1:
        score = 8.0
        reasoning = f"One hop from auth/config (auth={auth_distance}, config={config_distance})"
    elif min_distance == 2:
        score = 6.0
        reasoning = f"Two hops from auth/config (auth={auth_distance}, config={config_distance})"
    elif min_distance <= 4:
        score = 4.0
        reasoning = f"Moderate proximity to auth/config (distance={min_distance})"
    else:
        score = 2.0
        reasoning = f"Distant from auth/config (distance={min_distance})"

    return FactorScore(
        factor_name="auth_config_proximity",
        score=score,
        weight=FACTOR_WEIGHTS["auth_config_proximity"],
        reasoning=reasoning,
        evidence={
            "auth_distance": auth_distance,
            "config_distance": config_distance,
            "min_distance": min_distance if min_distance != float('inf') else -1,
        },
    )


def _score_execution_surface(
    file_path: str,
    context: FilePrioritizationContext,
) -> FactorScore:
    """Score file based on whether it's part of the execution surface.

    Execution surface files handle request processing:
    - Routes, handlers, controllers
    - Middleware, services
    - Workers, tasks
    """
    path = PurePosixPath(file_path)
    filename = path.name.lower()
    path_str = str(path).lower()

    score = 0.0
    reasoning_parts = []

    # Check filename/path for execution surface patterns
    for pattern in EXECUTION_SURFACE_PATTERNS:
        if pattern in filename or pattern in path_str:
            score = max(score, 8.0)
            reasoning_parts.append(f"Matches execution surface pattern '{pattern}'")

    # Check surface category map
    surface = context.surface_category_map.get(file_path, "")
    if surface in {"api", "auth"}:
        score = 10.0
        reasoning_parts.append(f"Surface category is '{surface}'")

    if not reasoning_parts:
        reasoning_parts.append("Not part of execution surface")
        score = 0.0

    return FactorScore(
        factor_name="execution_surface",
        score=score,
        weight=FACTOR_WEIGHTS["execution_surface"],
        reasoning="; ".join(reasoning_parts),
        evidence={"surface_category": surface, "path": path_str},
    )


def _score_dependency_relevance(
    file_path: str,
    context: FilePrioritizationContext,
) -> FactorScore:
    """Score file based on dependency relevance.

    Higher score for files that:
    - Are depended on by many files (high indegree)
    - Depend on few files (low outdegree)

    This identifies "hub" files that are critical to the system.
    """
    indegree = context.dependency_indegree.get(file_path, 0)
    outdegree = context.dependency_outdegree.get(file_path, 0)

    # Score based on indegree (files that depend on this)
    # High indegree = important file
    if indegree >= 10:
        indegree_score = 10.0
    elif indegree >= 5:
        indegree_score = 8.0
    elif indegree >= 3:
        indegree_score = 6.0
    elif indegree >= 1:
        indegree_score = 4.0
    else:
        indegree_score = 0.0

    # Score based on outdegree (files this depends on)
    # Low outdegree = less coupled, more stable
    if outdegree == 0:
        outdegree_score = 5.0  # Leaf file
    elif outdegree <= 2:
        outdegree_score = 4.0
    elif outdegree <= 5:
        outdegree_score = 3.0
    else:
        outdegree_score = 2.0  # Highly coupled

    # Combined score (indegree weighted higher)
    score = (indegree_score * 0.7) + (outdegree_score * 0.3)

    reasoning = f"Indegree={indegree} (importance), Outdegree={outdegree} (coupling)"
    if indegree >= 3:
        reasoning += f"; Hub file with {indegree} dependents"

    return FactorScore(
        factor_name="dependency_relevance",
        score=score,
        weight=FACTOR_WEIGHTS["dependency_relevance"],
        reasoning=reasoning,
        evidence={"indegree": indegree, "outdegree": outdegree},
    )


def _score_churn(
    file_path: str,
    context: FilePrioritizationContext,
) -> FactorScore:
    """Score file based on change frequency (churn).

    Higher churn may indicate:
    - Active development area
    - Potential instability
    - Higher risk for issues
    """
    # Use pre-computed churn score if available
    churn_score = context.churn_scores.get(file_path, -1)

    if churn_score >= 0:
        score = churn_score
        reasoning = f"Churn score from history: {churn_score:.1f}"
    else:
        # Fallback: infer from path patterns
        path = PurePosixPath(file_path)
        path_str = str(path).lower()

        # Check for high-churn patterns
        for pattern in HIGH_CHURN_PATTERNS:
            if pattern in path_str:
                score = 2.0  # Test files have lower priority
                reasoning = f"Test/fixture file pattern '{pattern}' - lower churn priority"
                break
        else:
            # Default: no churn data, use moderate score
            score = 5.0
            reasoning = "No churn data available, using default score"

    return FactorScore(
        factor_name="churn",
        score=score,
        weight=FACTOR_WEIGHTS["churn"],
        reasoning=reasoning,
        evidence={"churn_score": churn_score},
    )


def _score_exposure(
    file_path: str,
    context: FilePrioritizationContext,
) -> FactorScore:
    """Score file based on external exposure.

    Files that handle external input are higher priority:
    - API endpoints
    - Request handlers
    - File upload/download
    - Network communication
    """
    path = PurePosixPath(file_path)
    filename = path.name.lower()
    path_str = str(path).lower()

    score = 0.0
    reasoning_parts = []

    # Check for exposure patterns
    for pattern in EXPOSURE_PATTERNS:
        if pattern in filename or pattern in path_str:
            score = max(score, 8.0)
            reasoning_parts.append(f"Exposed to external input via '{pattern}'")

    # Check surface category
    surface = context.surface_category_map.get(file_path, "")
    if surface == "api":
        score = max(score, 10.0)
        reasoning_parts.append("API surface category")
    elif surface == "storage":
        score = max(score, 7.0)
        reasoning_parts.append("Storage surface (potential data exposure)")

    if not reasoning_parts:
        reasoning_parts.append("Not directly exposed to external input")
        score = 0.0

    return FactorScore(
        factor_name="exposure",
        score=score,
        weight=FACTOR_WEIGHTS["exposure"],
        reasoning="; ".join(reasoning_parts),
        evidence={"surface_category": surface, "path": path_str},
    )


# =============================================================================
# Main Scoring Functions
# =============================================================================

def priority_score(
    file_path: str,
    context: FilePrioritizationContext,
) -> FileScore:
    """Compute the prioritization score for a file.

    This is a PURE, DETERMINISTIC function.
    Same inputs always produce the same output.

    Args:
        file_path: Path to the file (relative to repo root)
        context: Context containing all data needed for scoring

    Returns:
        FileScore with total score, factor breakdown, and explanation
    """
    # Compute each factor score
    factor_scores = {
        "entrypoint": _score_entrypoint(file_path, context),
        "auth_config_proximity": _score_auth_config_proximity(file_path, context),
        "execution_surface": _score_execution_surface(file_path, context),
        "dependency_relevance": _score_dependency_relevance(file_path, context),
        "churn": _score_churn(file_path, context),
        "exposure": _score_exposure(file_path, context),
    }

    # Compute weighted total score
    total = sum(fs.score * fs.weight for fs in factor_scores.values())

    # Determine ranking tier
    if total >= 7.0:
        tier = "high"
    elif total >= 4.0:
        tier = "medium"
    else:
        tier = "low"

    # Determine primary reason
    max_factor = max(factor_scores.items(), key=lambda x: x[1].score)
    priority_reason = f"{max_factor[0]} ({max_factor[1].score:.1f})"

    return FileScore(
        file_path=file_path,
        total_score=total,
        factor_scores=factor_scores,
        ranking_tier=tier,
        priority_reason=priority_reason,
    )


def explain_score(file_score: FileScore) -> str:
    """Generate a human-readable explanation of a file's score.

    Args:
        file_score: The score to explain

    Returns:
        Human-readable explanation string
    """
    lines = [
        f"File: {file_score.file_path}",
        f"Total Score: {file_score.total_score:.2f} (Tier: {file_score.ranking_tier})",
        f"Primary Factor: {file_score.priority_reason}",
        "",
        "Factor Breakdown:",
    ]

    for factor_name, fs in sorted(
        file_score.factor_scores.items(),
        key=lambda x: x[1].score * x[1].weight,
        reverse=True,
    ):
        weighted = fs.score * fs.weight
        lines.append(f"  {factor_name}: {fs.score:.1f} × {fs.weight:.2f} = {weighted:.2f}")
        lines.append(f"    → {fs.reasoning}")

    return "\n".join(lines)


def prioritize_files(
    file_paths: list[str],
    context: FilePrioritizationContext,
    *,
    limit: int | None = None,
) -> list[tuple[str, FileScore]]:
    """Prioritize a list of files deterministically.

    Files are sorted by:
    1. Total score (descending)
    2. Deterministic tie-breaker using SHA256(snapshot_ref + file_path)

    Args:
        file_paths: List of file paths to prioritize
        context: Context containing all data needed for scoring
        limit: Optional limit on number of files to return

    Returns:
        List of (file_path, FileScore) tuples, sorted by priority
    """
    # Score all files
    scored = [(fp, priority_score(fp, context)) for fp in file_paths]

    # Sort by score (desc), then by deterministic key (asc) for ties
    sorted_files = sorted(
        scored,
        key=lambda x: (-x[1].total_score, context.get_deterministic_sort_key(x[0])),
    )

    if limit is not None:
        sorted_files = sorted_files[:limit]

    return sorted_files


# =============================================================================
# Context Builder
# =============================================================================

def build_empty_context(
    snapshot_ref: str,
    files: set[str],
) -> FilePrioritizationContext:
    """Build a minimal context with defaults for all fields.

    This is useful when no dependency or churn data is available.
    Files will be scored primarily on patterns and surface inference.

    Args:
        snapshot_ref: Reference to the snapshot
        files: Set of file paths in the snapshot

    Returns:
        FilePrioritizationContext with default values
    """
    # Infer surface categories from file paths
    surface_map: dict[str, str] = {}
    for file_path in files:
        path_lower = file_path.lower()
        if any(p in path_lower for p in AUTH_PATTERNS):
            surface_map[file_path] = "auth"
        elif any(p in path_lower for p in CONFIG_PATTERNS):
            surface_map[file_path] = "config"
        elif any(p in path_lower for p in EXECUTION_SURFACE_PATTERNS):
            surface_map[file_path] = "api"
        elif any(p in path_lower for p in {"db", "sql", "query", "storage"}):
            surface_map[file_path] = "storage"

    # Compute auth/config proximity from inferred surfaces
    auth_surfaces = {fp for fp, s in surface_map.items() if s == "auth"}
    config_surfaces = {fp for fp, s in surface_map.items() if s == "config"}

    auth_proximity = _compute_proximity(files, auth_surfaces)
    config_proximity = _compute_proximity(files, config_surfaces)

    return FilePrioritizationContext(
        snapshot_ref=snapshot_ref,
        files=frozenset(files),
        auth_proximity_map=auth_proximity,
        config_proximity_map=config_proximity,
        dependency_indegree={},
        dependency_outdegree={},
        churn_scores={},
        surface_category_map=surface_map,
    )


def _compute_proximity(
    all_files: set[str],
    surface_files: set[str],
) -> dict[str, int]:
    """Compute proximity from each file to nearest surface file.

    This is a simplified implementation that uses path-based proximity:
    - Distance 0: File is a surface file
    - Distance 1: File is in the same directory as a surface file
    - Distance 2: File is in a subdirectory of a surface directory
    - Distance N: Based on path depth difference

    Args:
        all_files: All files to compute proximity for
        surface_files: Files that are part of the surface

    Returns:
        Map of file path → distance to nearest surface
    """
    proximity: dict[str, int] = {}

    # Get directories containing surface files
    surface_dirs = set()
    for sf in surface_files:
        path = PurePosixPath(sf)
        surface_dirs.add(str(path.parent))

    for file_path in all_files:
        if file_path in surface_files:
            proximity[file_path] = 0
            continue

        path = PurePosixPath(file_path)
        parent = str(path.parent)

        # Check if in same directory as surface file
        if parent in surface_dirs:
            proximity[file_path] = 1
            continue

        # Check if in subdirectory
        min_distance = -1
        for surface_dir in surface_dirs:
            if parent.startswith(surface_dir + "/"):
                depth = len(parent[len(surface_dir) + 1:].split("/"))
                if min_distance < 0 or depth < min_distance:
                    min_distance = depth + 1

        proximity[file_path] = min_distance if min_distance >= 0 else -1

    return proximity


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Dataclasses
    "FactorScore",
    "FileScore",
    "FilePrioritizationContext",
    # Functions
    "priority_score",
    "explain_score",
    "prioritize_files",
    "build_empty_context",
    # Constants
    "FACTOR_WEIGHTS",
    "MAX_FACTOR_SCORE",
]
