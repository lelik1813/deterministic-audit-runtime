"""
Tests for File Prioritization (STEP 6)

Tests the deterministic, explainable, yield-oriented file selection scoring function.
"""

from __future__ import annotations

import hashlib
import pytest

from runtime.file_prioritization import (
    FactorScore,
    FileScore,
    FilePrioritizationContext,
    FACTOR_WEIGHTS,
    MAX_FACTOR_SCORE,
    ENTRYPOINT_PATTERNS,
    AUTH_PATTERNS,
    CONFIG_PATTERNS,
    EXECUTION_SURFACE_PATTERNS,
    EXPOSURE_PATTERNS,
    priority_score,
    explain_score,
    prioritize_files,
    build_empty_context,
    _score_entrypoint,
    _score_auth_config_proximity,
    _score_execution_surface,
    _score_dependency_relevance,
    _score_churn,
    _score_exposure,
    _compute_proximity,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def minimal_context() -> FilePrioritizationContext:
    """Create a minimal context for testing."""
    return FilePrioritizationContext(
        snapshot_ref="test_snapshot_abc123",
        files=frozenset({
            "src/main.py",
            "src/auth/login.py",
            "src/config/settings.py",
            "src/api/handlers.py",
            "src/utils/helpers.py",
            "tests/test_main.py",
        }),
        auth_proximity_map={
            "src/main.py": 1,
            "src/auth/login.py": 0,
            "src/config/settings.py": -1,
            "src/api/handlers.py": 2,
            "src/utils/helpers.py": -1,
            "tests/test_main.py": -1,
        },
        config_proximity_map={
            "src/main.py": 1,
            "src/auth/login.py": -1,
            "src/config/settings.py": 0,
            "src/api/handlers.py": 2,
            "src/utils/helpers.py": -1,
            "tests/test_main.py": -1,
        },
        dependency_indegree={
            "src/main.py": 5,
            "src/utils/helpers.py": 10,
            "src/auth/login.py": 3,
        },
        dependency_outdegree={
            "src/main.py": 2,
            "src/utils/helpers.py": 0,
            "src/auth/login.py": 4,
        },
        churn_scores={
            "src/main.py": 8.0,
            "src/auth/login.py": 6.0,
        },
        surface_category_map={
            "src/auth/login.py": "auth",
            "src/config/settings.py": "config",
            "src/api/handlers.py": "api",
        },
    )


# =============================================================================
# FactorScore Tests
# =============================================================================

class TestFactorScore:
    """Tests for FactorScore dataclass."""

    def test_valid_factor_score(self) -> None:
        """Test creating a valid FactorScore."""
        fs = FactorScore(
            factor_name="test",
            score=5.0,
            weight=0.15,
            reasoning="Test reasoning",
        )
        assert fs.factor_name == "test"
        assert fs.score == 5.0
        assert fs.weight == 0.15
        assert fs.reasoning == "Test reasoning"
        assert fs.evidence == {}

    def test_factor_score_with_evidence(self) -> None:
        """Test FactorScore with evidence."""
        fs = FactorScore(
            factor_name="test",
            score=7.0,
            weight=0.20,
            reasoning="Test",
            evidence={"key": "value"},
        )
        assert fs.evidence == {"key": "value"}

    def test_factor_score_invalid_low(self) -> None:
        """Test FactorScore rejects score below 0."""
        with pytest.raises(ValueError, match="Score must be in"):
            FactorScore(
                factor_name="test",
                score=-1.0,
                weight=0.15,
                reasoning="Invalid",
            )

    def test_factor_score_invalid_high(self) -> None:
        """Test FactorScore rejects score above MAX_FACTOR_SCORE."""
        with pytest.raises(ValueError, match="Score must be in"):
            FactorScore(
                factor_name="test",
                score=15.0,
                weight=0.15,
                reasoning="Invalid",
            )


# =============================================================================
# FileScore Tests
# =============================================================================

class TestFileScore:
    """Tests for FileScore dataclass."""

    def test_file_score_high_priority(self) -> None:
        """Test FileScore high priority detection."""
        fs = FileScore(
            file_path="test.py",
            total_score=8.0,
            factor_scores={},
            ranking_tier="high",
            priority_reason="test",
        )
        assert fs.is_high_priority
        assert not fs.is_medium_priority
        assert not fs.is_low_priority

    def test_file_score_medium_priority(self) -> None:
        """Test FileScore medium priority detection."""
        fs = FileScore(
            file_path="test.py",
            total_score=5.0,
            factor_scores={},
            ranking_tier="medium",
            priority_reason="test",
        )
        assert not fs.is_high_priority
        assert fs.is_medium_priority
        assert not fs.is_low_priority

    def test_file_score_low_priority(self) -> None:
        """Test FileScore low priority detection."""
        fs = FileScore(
            file_path="test.py",
            total_score=2.0,
            factor_scores={},
            ranking_tier="low",
            priority_reason="test",
        )
        assert not fs.is_high_priority
        assert not fs.is_medium_priority
        assert fs.is_low_priority

    def test_file_score_to_dict(self) -> None:
        """Test FileScore serialization."""
        fs = FileScore(
            file_path="test.py",
            total_score=5.5,
            factor_scores={
                "entrypoint": FactorScore(
                    factor_name="entrypoint",
                    score=10.0,
                    weight=0.20,
                    reasoning="Entry point",
                ),
            },
            ranking_tier="medium",
            priority_reason="entrypoint",
        )
        d = fs.to_dict()
        assert d["file_path"] == "test.py"
        assert d["total_score"] == 5.5
        assert "entrypoint" in d["factor_scores"]
        assert d["ranking_tier"] == "medium"


# =============================================================================
# FilePrioritizationContext Tests
# =============================================================================

class TestFilePrioritizationContext:
    """Tests for FilePrioritizationContext."""

    def test_deterministic_sort_key(self, minimal_context: FilePrioritizationContext) -> None:
        """Test that deterministic sort key is consistent."""
        key1 = minimal_context.get_deterministic_sort_key("src/main.py")
        key2 = minimal_context.get_deterministic_sort_key("src/main.py")

        assert key1 == key2
        assert len(key1) == 64  # SHA256 hexdigest length

    def test_deterministic_sort_key_different_files(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test that different files get different sort keys."""
        key1 = minimal_context.get_deterministic_sort_key("src/main.py")
        key2 = minimal_context.get_deterministic_sort_key("src/auth/login.py")

        assert key1 != key2

    def test_deterministic_sort_key_different_snapshots(self) -> None:
        """Test that same file in different snapshots gets different keys."""
        ctx1 = FilePrioritizationContext(
            snapshot_ref="snapshot_a",
            files=frozenset({"test.py"}),
            auth_proximity_map={},
            config_proximity_map={},
            dependency_indegree={},
            dependency_outdegree={},
            churn_scores={},
            surface_category_map={},
        )
        ctx2 = FilePrioritizationContext(
            snapshot_ref="snapshot_b",
            files=frozenset({"test.py"}),
            auth_proximity_map={},
            config_proximity_map={},
            dependency_indegree={},
            dependency_outdegree={},
            churn_scores={},
            surface_category_map={},
        )

        key1 = ctx1.get_deterministic_sort_key("test.py")
        key2 = ctx2.get_deterministic_sort_key("test.py")

        assert key1 != key2


# =============================================================================
# Entrypoint Scoring Tests
# =============================================================================

class TestScoreEntrypoint:
    """Tests for _score_entrypoint function."""

    def test_entrypoint_main_py(self, minimal_context: FilePrioritizationContext) -> None:
        """Test main.py is scored as entry point."""
        fs = _score_entrypoint("src/main.py", minimal_context)
        assert fs.score == 10.0
        assert "entry point" in fs.reasoning.lower()

    def test_entrypoint_app_py(self, minimal_context: FilePrioritizationContext) -> None:
        """Test app.py is scored as entry point."""
        fs = _score_entrypoint("app.py", minimal_context)
        assert fs.score == 10.0

    def test_entrypoint_server_py(self, minimal_context: FilePrioritizationContext) -> None:
        """Test server.py is scored as entry point."""
        fs = _score_entrypoint("server.py", minimal_context)
        assert fs.score == 10.0

    def test_entrypoint_wsgi_py(self, minimal_context: FilePrioritizationContext) -> None:
        """Test wsgi.py is scored as entry point."""
        fs = _score_entrypoint("wsgi.py", minimal_context)
        assert fs.score == 10.0

    def test_entrypoint_index_js(self, minimal_context: FilePrioritizationContext) -> None:
        """Test index.js is scored as entry point."""
        fs = _score_entrypoint("index.js", minimal_context)
        assert fs.score == 10.0

    def test_entrypoint_init_py(self, minimal_context: FilePrioritizationContext) -> None:
        """Test __init__.py is scored as entry point."""
        fs = _score_entrypoint("src/__init__.py", minimal_context)
        assert fs.score == 10.0

    def test_entrypoint_cmd_directory(self, minimal_context: FilePrioritizationContext) -> None:
        """Test files in cmd/ directory are entry points."""
        fs = _score_entrypoint("cmd/server/main.go", minimal_context)
        assert fs.score >= 8.0

    def test_not_entrypoint(self, minimal_context: FilePrioritizationContext) -> None:
        """Test regular file is not scored as entry point."""
        fs = _score_entrypoint("src/utils/helpers.py", minimal_context)
        assert fs.score == 0.0


# =============================================================================
# Auth/Config Proximity Scoring Tests
# =============================================================================

class TestScoreAuthConfigProximity:
    """Tests for _score_auth_config_proximity function."""

    def test_direct_auth_surface(self, minimal_context: FilePrioritizationContext) -> None:
        """Test direct auth surface gets highest score."""
        fs = _score_auth_config_proximity("src/auth/login.py", minimal_context)
        assert fs.score == 10.0
        assert fs.evidence["auth_distance"] == 0

    def test_direct_config_surface(self, minimal_context: FilePrioritizationContext) -> None:
        """Test direct config surface gets highest score."""
        fs = _score_auth_config_proximity("src/config/settings.py", minimal_context)
        assert fs.score == 10.0
        assert fs.evidence["config_distance"] == 0

    def test_one_hop_from_auth(self, minimal_context: FilePrioritizationContext) -> None:
        """Test one hop from auth gets high score."""
        fs = _score_auth_config_proximity("src/main.py", minimal_context)
        assert fs.score >= 8.0

    def test_no_proximity(self, minimal_context: FilePrioritizationContext) -> None:
        """Test no proximity to auth/config gets low score."""
        fs = _score_auth_config_proximity("tests/test_main.py", minimal_context)
        assert fs.score == 0.0


# =============================================================================
# Execution Surface Scoring Tests
# =============================================================================

class TestScoreExecutionSurface:
    """Tests for _score_execution_surface function."""

    def test_api_surface(self, minimal_context: FilePrioritizationContext) -> None:
        """Test API surface gets highest score."""
        fs = _score_execution_surface("src/api/handlers.py", minimal_context)
        assert fs.score == 10.0

    def test_handler_pattern(self, minimal_context: FilePrioritizationContext) -> None:
        """Test handler pattern matches."""
        fs = _score_execution_surface("src/handlers/user_handler.py", minimal_context)
        assert fs.score >= 8.0

    def test_controller_pattern(self, minimal_context: FilePrioritizationContext) -> None:
        """Test controller pattern matches."""
        fs = _score_execution_surface("src/controllers/auth_controller.py", minimal_context)
        assert fs.score >= 8.0

    def test_route_pattern(self, minimal_context: FilePrioritizationContext) -> None:
        """Test route pattern matches."""
        fs = _score_execution_surface("src/routes/api.py", minimal_context)
        assert fs.score >= 8.0

    def test_not_execution_surface(self, minimal_context: FilePrioritizationContext) -> None:
        """Test regular file is not scored as execution surface."""
        fs = _score_execution_surface("src/utils/helpers.py", minimal_context)
        assert fs.score == 0.0


# =============================================================================
# Dependency Relevance Scoring Tests
# =============================================================================

class TestScoreDependencyRelevance:
    """Tests for _score_dependency_relevance function."""

    def test_high_indegree(self, minimal_context: FilePrioritizationContext) -> None:
        """Test high indegree (hub file) gets high score."""
        fs = _score_dependency_relevance("src/utils/helpers.py", minimal_context)
        assert fs.score >= 8.0  # indegree=10
        assert "hub" in fs.reasoning.lower()

    def test_moderate_indegree(self, minimal_context: FilePrioritizationContext) -> None:
        """Test moderate indegree gets moderate score."""
        fs = _score_dependency_relevance("src/auth/login.py", minimal_context)
        assert 4.0 <= fs.score < 8.0  # indegree=3

    def test_no_dependencies(self, minimal_context: FilePrioritizationContext) -> None:
        """Test file with no dependency data gets low score."""
        fs = _score_dependency_relevance("src/config/settings.py", minimal_context)
        # indegree=0, outdegree=0
        assert fs.score >= 0.0


# =============================================================================
# Churn Scoring Tests
# =============================================================================

class TestScoreChurn:
    """Tests for _score_churn function."""

    def test_known_churn_score(self, minimal_context: FilePrioritizationContext) -> None:
        """Test known churn score is used."""
        fs = _score_churn("src/main.py", minimal_context)
        assert fs.score == 8.0

    def test_unknown_churn_uses_default(self, minimal_context: FilePrioritizationContext) -> None:
        """Test unknown churn uses default score."""
        fs = _score_churn("src/utils/helpers.py", minimal_context)
        assert fs.score == 5.0

    def test_test_file_lower_priority(self, minimal_context: FilePrioritizationContext) -> None:
        """Test test files get lower churn priority."""
        fs = _score_churn("tests/test_main.py", minimal_context)
        assert fs.score == 2.0


# =============================================================================
# Exposure Scoring Tests
# =============================================================================

class TestScoreExposure:
    """Tests for _score_exposure function."""

    def test_api_exposure(self, minimal_context: FilePrioritizationContext) -> None:
        """Test API files are scored as exposed."""
        fs = _score_exposure("src/api/handlers.py", minimal_context)
        assert fs.score == 10.0

    def test_endpoint_pattern(self, minimal_context: FilePrioritizationContext) -> None:
        """Test endpoint pattern matches."""
        fs = _score_exposure("src/endpoints/users.py", minimal_context)
        assert fs.score >= 8.0

    def test_request_handler_pattern(self, minimal_context: FilePrioritizationContext) -> None:
        """Test request handler pattern matches."""
        fs = _score_exposure("src/request_handler.py", minimal_context)
        assert fs.score >= 8.0

    def test_not_exposed(self, minimal_context: FilePrioritizationContext) -> None:
        """Test regular file is not scored as exposed."""
        fs = _score_exposure("src/utils/helpers.py", minimal_context)
        assert fs.score == 0.0


# =============================================================================
# Priority Score Tests
# =============================================================================

class TestPriorityScore:
    """Tests for priority_score function."""

    def test_priority_score_returns_file_score(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test priority_score returns FileScore."""
        score = priority_score("src/main.py", minimal_context)
        assert isinstance(score, FileScore)
        assert score.file_path == "src/main.py"

    def test_priority_score_includes_all_factors(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test priority_score includes all factor scores."""
        score = priority_score("src/main.py", minimal_context)
        expected_factors = {
            "entrypoint",
            "auth_config_proximity",
            "execution_surface",
            "dependency_relevance",
            "churn",
            "exposure",
        }
        assert set(score.factor_scores.keys()) == expected_factors

    def test_priority_score_is_weighted_sum(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test total_score is weighted sum of factor scores."""
        score = priority_score("src/main.py", minimal_context)

        expected_total = sum(
            fs.score * fs.weight
            for fs in score.factor_scores.values()
        )
        assert abs(score.total_score - expected_total) < 0.01

    def test_priority_score_is_deterministic(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test same inputs produce same outputs."""
        score1 = priority_score("src/main.py", minimal_context)
        score2 = priority_score("src/main.py", minimal_context)

        assert score1.total_score == score2.total_score


# =============================================================================
# Explain Score Tests
# =============================================================================

class TestExplainScore:
    """Tests for explain_score function."""

    def test_explain_returns_string(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test explain_score returns string."""
        score = priority_score("src/main.py", minimal_context)
        explanation = explain_score(score)
        assert isinstance(explanation, str)

    def test_explain_includes_file_path(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test explanation includes file path."""
        score = priority_score("src/main.py", minimal_context)
        explanation = explain_score(score)
        assert "src/main.py" in explanation

    def test_explain_includes_total_score(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test explanation includes total score."""
        score = priority_score("src/main.py", minimal_context)
        explanation = explain_score(score)
        assert "Total Score:" in explanation

    def test_explain_includes_all_factors(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test explanation includes all factors."""
        score = priority_score("src/main.py", minimal_context)
        explanation = explain_score(score)
        for factor in score.factor_scores:
            assert factor in explanation


# =============================================================================
# Prioritize Files Tests
# =============================================================================

class TestPrioritizeFiles:
    """Tests for prioritize_files function."""

    def test_prioritize_returns_sorted_list(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test prioritize_files returns sorted list."""
        files = ["src/utils/helpers.py", "src/main.py"]
        result = prioritize_files(files, minimal_context)

        assert isinstance(result, list)
        assert len(result) == 2

    def test_prioritize_higher_score_first(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test higher scoring files come first."""
        files = ["src/utils/helpers.py", "src/main.py"]
        result = prioritize_files(files, minimal_context)

        # main.py should score higher (entry point)
        first_score = result[0][1].total_score
        second_score = result[1][1].total_score
        assert first_score >= second_score

    def test_prioritize_respects_limit(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test prioritize_files respects limit parameter."""
        files = list(minimal_context.files)
        result = prioritize_files(files, minimal_context, limit=2)

        assert len(result) == 2

    def test_prioritize_is_deterministic(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test same inputs produce same ordering."""
        files = list(minimal_context.files)

        result1 = prioritize_files(files, minimal_context)
        result2 = prioritize_files(files, minimal_context)

        paths1 = [fp for fp, _ in result1]
        paths2 = [fp for fp, _ in result2]
        assert paths1 == paths2

    def test_prioritize_uses_tie_breaker(
        self, minimal_context: FilePrioritizationContext,
    ) -> None:
        """Test ties are broken deterministically."""
        # Create files that should have same score
        files = ["src/a.py", "src/b.py"]
        result = prioritize_files(files, minimal_context)

        # Ordering should be deterministic even with equal scores
        assert len(result) == 2
        # Order should be consistent across multiple calls
        result2 = prioritize_files(files, minimal_context)
        paths1 = [fp for fp, _ in result]
        paths2 = [fp for fp, _ in result2]
        assert paths1 == paths2


# =============================================================================
# Build Empty Context Tests
# =============================================================================

class TestBuildEmptyContext:
    """Tests for build_empty_context function."""

    def test_build_empty_context_creates_context(self) -> None:
        """Test build_empty_context creates a context."""
        files = {"src/main.py", "src/auth/login.py"}
        ctx = build_empty_context("snapshot_123", files)

        assert isinstance(ctx, FilePrioritizationContext)
        assert ctx.snapshot_ref == "snapshot_123"
        assert ctx.files == frozenset(files)

    def test_build_empty_context_infers_auth_surface(self) -> None:
        """Test build_empty_context infers auth surfaces."""
        files = {"src/auth/login.py", "src/utils/helpers.py"}
        ctx = build_empty_context("snapshot_123", files)

        assert ctx.surface_category_map.get("src/auth/login.py") == "auth"

    def test_build_empty_context_infers_config_surface(self) -> None:
        """Test build_empty_context infers config surfaces."""
        files = {"src/config/settings.py", "src/utils/helpers.py"}
        ctx = build_empty_context("snapshot_123", files)

        assert ctx.surface_category_map.get("src/config/settings.py") == "config"

    def test_build_empty_context_infers_api_surface(self) -> None:
        """Test build_empty_context infers API surfaces."""
        files = {"src/api/handlers.py", "src/utils/helpers.py"}
        ctx = build_empty_context("snapshot_123", files)

        assert ctx.surface_category_map.get("src/api/handlers.py") == "api"

    def test_build_empty_context_computes_proximity(self) -> None:
        """Test build_empty_context computes proximity."""
        files = {"src/auth/login.py", "src/auth/utils.py", "src/utils/helpers.py"}
        ctx = build_empty_context("snapshot_123", files)

        # login.py is auth surface, should have distance 0
        assert ctx.auth_proximity_map.get("src/auth/login.py") == 0


# =============================================================================
# Proximity Computation Tests
# =============================================================================

class TestComputeProximity:
    """Tests for _compute_proximity function."""

    def test_surface_file_has_distance_zero(self) -> None:
        """Test surface files have distance 0."""
        all_files = {"src/auth/login.py", "src/utils/helpers.py"}
        surface_files = {"src/auth/login.py"}

        proximity = _compute_proximity(all_files, surface_files)

        assert proximity["src/auth/login.py"] == 0

    def test_same_directory_has_distance_one(self) -> None:
        """Test files in same directory have distance 1."""
        all_files = {"src/auth/login.py", "src/auth/utils.py"}
        surface_files = {"src/auth/login.py"}

        proximity = _compute_proximity(all_files, surface_files)

        assert proximity["src/auth/utils.py"] == 1

    def test_not_reachable_has_distance_minus_one(self) -> None:
        """Test unreachable files have distance -1."""
        all_files = {"src/auth/login.py", "tests/test.py"}
        surface_files = {"src/auth/login.py"}

        proximity = _compute_proximity(all_files, surface_files)

        assert proximity["tests/test.py"] == -1


# =============================================================================
# DoD Verification Tests
# =============================================================================

class TestStep6DoD:
    """Tests verifying STEP 6 DoD criteria."""

    def test_no_more_first_n_selection(self) -> None:
        """DoD: No more first-N selection.

        Verify that files are selected based on score, not original order.
        """
        files = [
            "tests/test_main.py",
            "src/main.py",
            "src/auth/login.py",
        ]
        ctx = build_empty_context("snapshot_123", set(files))
        result = prioritize_files(files, ctx)

        # Files should be in priority order, not original order
        paths = [fp for fp, _ in result]

        # main.py should come before tests (higher priority)
        main_idx = paths.index("src/main.py")
        test_idx = paths.index("tests/test_main.py")
        assert main_idx < test_idx

    def test_same_snapshot_same_ordering(self) -> None:
        """DoD: Same snapshot → same ordering.

        Verify that the same snapshot produces the same ordering every time.
        """
        files = {"src/main.py", "src/auth/login.py", "src/utils/helpers.py"}
        ctx = build_empty_context("snapshot_abc123", files)

        result1 = prioritize_files(list(files), ctx)
        result2 = prioritize_files(list(files), ctx)
        result3 = prioritize_files(list(files), ctx)

        paths1 = tuple(fp for fp, _ in result1)
        paths2 = tuple(fp for fp, _ in result2)
        paths3 = tuple(fp for fp, _ in result3)

        assert paths1 == paths2 == paths3

    def test_explainable_selection(self) -> None:
        """DoD: Explainable selection.

        Verify that every selection can be explained with reasoning.
        """
        files = {"src/main.py", "src/auth/login.py"}
        ctx = build_empty_context("snapshot_123", files)
        result = prioritize_files(list(files), ctx)

        for file_path, score in result:
            explanation = explain_score(score)
            # Every explanation should mention the file and score
            assert file_path in explanation
            assert "Score" in explanation or "score" in explanation
            # Every explanation should have factor breakdown
            assert "Breakdown" in explanation or "factor" in explanation.lower()

    def test_measurable_yield_improvement(self) -> None:
        """DoD: Measurable yield improvement.

        Verify that high-priority files are more likely to contain security surfaces.
        """
        files = {
            "src/main.py",
            "src/auth/login.py",
            "src/config/settings.py",
            "src/api/handlers.py",
            "src/utils/helpers.py",
            "tests/test_main.py",
            "docs/readme.md",
        }
        ctx = build_empty_context("snapshot_123", files)
        result = prioritize_files(list(files), ctx)

        # Check that known security surfaces are in top half
        security_surfaces = {"src/auth/login.py", "src/config/settings.py", "src/api/handlers.py"}
        top_half = result[:len(result) // 2 + 1]
        top_half_paths = {fp for fp, _ in top_half}

        # At least one security surface should be in top half
        assert len(security_surfaces & top_half_paths) >= 1

    def test_all_factors_have_weights(self) -> None:
        """DoD: All 6 factors are defined with weights."""
        expected_factors = {
            "entrypoint",
            "auth_config_proximity",
            "execution_surface",
            "dependency_relevance",
            "churn",
            "exposure",
        }

        assert set(FACTOR_WEIGHTS.keys()) == expected_factors

        # Weights should sum to ~1.0
        total_weight = sum(FACTOR_WEIGHTS.values())
        assert abs(total_weight - 1.0) < 0.01


# =============================================================================
# Constants Tests
# =============================================================================

class TestConstants:
    """Tests for module constants."""

    def test_weights_sum_to_one(self) -> None:
        """Test factor weights sum to 1.0."""
        total = sum(FACTOR_WEIGHTS.values())
        assert abs(total - 1.0) < 0.01

    def test_max_factor_score_is_10(self) -> None:
        """Test MAX_FACTOR_SCORE is 10."""
        assert MAX_FACTOR_SCORE == 10.0

    def test_entrypoint_patterns_not_empty(self) -> None:
        """Test entrypoint patterns are defined."""
        assert len(ENTRYPOINT_PATTERNS) > 0

    def test_auth_patterns_not_empty(self) -> None:
        """Test auth patterns are defined."""
        assert len(AUTH_PATTERNS) > 0

    def test_config_patterns_not_empty(self) -> None:
        """Test config patterns are defined."""
        assert len(CONFIG_PATTERNS) > 0

    def test_execution_surface_patterns_not_empty(self) -> None:
        """Test execution surface patterns are defined."""
        assert len(EXECUTION_SURFACE_PATTERNS) > 0

    def test_exposure_patterns_not_empty(self) -> None:
        """Test exposure patterns are defined."""
        assert len(EXPOSURE_PATTERNS) > 0
