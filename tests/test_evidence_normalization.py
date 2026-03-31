"""
Unit tests for Evidence Normalization (STEP 1 - Schema v1.3)

These tests verify that:
1. Missing line_start/line_end no longer breaks transport
2. All fallback ranges are marked with range_inferred=true
3. No silent fake precision
4. NormalizedSourceRef is transport-valid with explicit typing
"""

from __future__ import annotations

import pytest

from runtime.evidence import (
    DEFAULT_LINE_END,
    DEFAULT_LINE_START,
    EVIDENCE_SCHEMA_VERSION,
    NormalizedSourceRef,
    normalize_source_ref,
    normalize_source_refs,
)
from runtime.output_normalizer import OutputNormalizer


class TestEvidenceSchemaVersion:
    """Tests for Schema v1.3 version constant."""

    def test_schema_version_defined(self) -> None:
        """Verify evidence schema version is defined."""
        assert EVIDENCE_SCHEMA_VERSION == "1.3.0"

    def test_default_line_start(self) -> None:
        """Verify default line start is 1."""
        assert DEFAULT_LINE_START == 1

    def test_default_line_end(self) -> None:
        """Verify default line end is 1."""
        assert DEFAULT_LINE_END == 1


class TestNormalizedSourceRef:
    """Tests for NormalizedSourceRef dataclass."""

    def test_basic_source_ref(self) -> None:
        """Verify basic source ref creation."""
        ref = NormalizedSourceRef(
            file_path="src/app.py",
            snapshot_ref="abc123",
            line_range_start=10,
            line_range_end=20,
        )
        assert ref.file_path == "src/app.py"
        assert ref.snapshot_ref == "abc123"
        assert ref.line_range_start == 10
        assert ref.line_range_end == 20
        assert ref.range_inferred is False
        assert ref.normalization_warning is None

    def test_inferred_range_flag(self) -> None:
        """Verify range_inferred flag is set correctly."""
        ref = NormalizedSourceRef(
            file_path="src/app.py",
            snapshot_ref="abc123",
            line_range_start=1,
            line_range_end=1,
            range_inferred=True,
            normalization_warning="line_range missing",
        )
        assert ref.range_inferred is True
        assert ref.normalization_warning == "line_range missing"
        assert ref.is_degraded is True

    def test_to_dict_includes_required_fields(self) -> None:
        """Verify serialization includes all required fields."""
        ref = NormalizedSourceRef(
            file_path="src/app.py",
            snapshot_ref="abc123",
            line_range_start=10,
            line_range_end=20,
        )
        d = ref.to_dict()
        assert d["file_path"] == "src/app.py"
        assert d["snapshot_ref"] == "abc123"
        assert d["line_range"] == {"start": 10, "end": 20}
        assert d["range_inferred"] is False
        assert "normalization_warning" not in d  # None should not be included

    def test_to_dict_includes_warning_when_present(self) -> None:
        """Verify serialization includes warning when present."""
        ref = NormalizedSourceRef(
            file_path="src/app.py",
            snapshot_ref="abc123",
            line_range_start=1,
            line_range_end=1,
            range_inferred=True,
            normalization_warning="line_range.start missing",
        )
        d = ref.to_dict()
        assert d["range_inferred"] is True
        assert d["normalization_warning"] == "line_range.start missing"

    def test_is_degraded_property(self) -> None:
        """Verify is_degraded property works correctly."""
        normal_ref = NormalizedSourceRef(
            file_path="src/app.py",
            snapshot_ref="abc123",
            line_range_start=10,
            line_range_end=20,
        )
        assert normal_ref.is_degraded is False

        degraded_ref = NormalizedSourceRef(
            file_path="src/app.py",
            snapshot_ref="abc123",
            line_range_start=1,
            line_range_end=1,
            range_inferred=True,
        )
        assert degraded_ref.is_degraded is True


class TestNormalizeSourceRef:
    """Tests for normalize_source_ref function."""

    def test_complete_source_ref(self) -> None:
        """Verify complete source ref passes through unchanged."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
            "line_range": {"start": 10, "end": 20},
        })
        assert result is not None
        assert result.file_path == "src/app.py"
        assert result.snapshot_ref == "abc123"
        assert result.line_range_start == 10
        assert result.line_range_end == 20
        assert result.range_inferred is False
        assert result.normalization_warning is None

    def test_missing_line_range(self) -> None:
        """Verify missing line_range gets defaults with inferred flag."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
        })
        assert result is not None
        assert result.line_range_start == DEFAULT_LINE_START
        assert result.line_range_end == DEFAULT_LINE_END
        assert result.range_inferred is True
        assert "line_range missing" in result.normalization_warning

    def test_missing_line_start(self) -> None:
        """Verify missing line_start gets default with inferred flag."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
            "line_range": {"end": 20},
        })
        assert result is not None
        assert result.line_range_start == DEFAULT_LINE_START
        assert result.line_range_end == 20
        assert result.range_inferred is True
        assert "line_range.start" in result.normalization_warning

    def test_missing_line_end(self) -> None:
        """Verify missing line_end defaults to start."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
            "line_range": {"start": 10},
        })
        assert result is not None
        assert result.line_range_start == 10
        assert result.line_range_end == 10  # Defaults to start
        assert result.range_inferred is True
        assert "line_range.end" in result.normalization_warning

    def test_invalid_line_start_type(self) -> None:
        """Verify invalid line_start type gets default."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
            "line_range": {"start": "ten", "end": 20},
        })
        assert result is not None
        assert result.line_range_start == DEFAULT_LINE_START
        assert result.range_inferred is True

    def test_invalid_line_start_negative(self) -> None:
        """Verify negative line_start gets default."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
            "line_range": {"start": -5, "end": 20},
        })
        assert result is not None
        assert result.line_range_start == DEFAULT_LINE_START
        assert result.range_inferred is True

    def test_start_greater_than_end(self) -> None:
        """Verify start > end is normalized to start = end."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
            "line_range": {"start": 30, "end": 10},
        })
        assert result is not None
        assert result.line_range_start == 30
        assert result.line_range_end == 30  # Normalized to start
        assert result.range_inferred is True

    def test_missing_file_path_returns_none(self) -> None:
        """Verify missing file_path returns None."""
        result = normalize_source_ref({
            "snapshot_ref": "abc123",
            "line_range": {"start": 10, "end": 20},
        })
        assert result is None

    def test_missing_snapshot_ref_returns_none(self) -> None:
        """Verify missing snapshot_ref returns None."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "line_range": {"start": 10, "end": 20},
        })
        assert result is None

    def test_empty_file_path_returns_none(self) -> None:
        """Verify empty file_path returns None."""
        result = normalize_source_ref({
            "file_path": "   ",
            "snapshot_ref": "abc123",
        })
        assert result is None

    def test_path_normalization(self) -> None:
        """Verify backslashes are converted to forward slashes."""
        result = normalize_source_ref({
            "file_path": "src\\nested\\app.py",
            "snapshot_ref": "abc123",
        })
        assert result is not None
        assert result.file_path == "src/nested/app.py"

    def test_file_hash_preserved(self) -> None:
        """Verify file_hash is preserved and normalized."""
        result = normalize_source_ref({
            "file_path": "src/app.py",
            "snapshot_ref": "abc123",
            "line_range": {"start": 10, "end": 20},
            "file_hash": "ABC123DEF",
        })
        assert result is not None
        assert result.file_hash == "abc123def"  # Lowercased


class TestNormalizeSourceRefs:
    """Tests for normalize_source_refs list function."""

    def test_normalizes_list(self) -> None:
        """Verify list of source refs is normalized."""
        results = normalize_source_refs([
            {
                "file_path": "src/a.py",
                "snapshot_ref": "ref1",
                "line_range": {"start": 10, "end": 20},
            },
            {
                "file_path": "src/b.py",
                "snapshot_ref": "ref2",
                # Missing line_range
            },
        ])
        assert len(results) == 2
        assert results[0].range_inferred is False
        assert results[1].range_inferred is True

    def test_filters_invalid_refs(self) -> None:
        """Verify invalid refs are filtered out."""
        results = normalize_source_refs([
            {
                "file_path": "src/a.py",
                "snapshot_ref": "ref1",
            },
            {
                # Missing file_path
                "snapshot_ref": "ref2",
            },
            "not a dict",
        ])
        assert len(results) == 1
        assert results[0].file_path == "src/a.py"

    def test_empty_list(self) -> None:
        """Verify empty list returns empty."""
        results = normalize_source_refs([])
        assert results == []

    def test_none_input(self) -> None:
        """Verify None input returns empty list."""
        results = normalize_source_refs(None)
        assert results == []


class TestOutputNormalizerEvidence:
    """Tests for OutputNormalizer evidence normalization (STEP 1)."""

    def test_normalizes_source_refs_in_observation(self) -> None:
        """Verify source_refs in observation events are normalized."""
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            raw_output="""{
                "candidate_events": [{
                    "event_type": "observation.proposed",
                    "entity_type": "observation",
                    "snapshot_ref": "snap1",
                    "payload": {
                        "claim": "Test",
                        "provenance": {
                            "source_refs": [{
                                "file_path": "src/app.py",
                                "snapshot_ref": "snap1"
                            }]
                        }
                    }
                }]
            }"""
        )
        assert result.success
        source_ref = result.candidate_events[0]["payload"]["provenance"]["source_refs"][0]
        assert "line_range" in source_ref
        assert source_ref["range_inferred"] is True

    def test_normalizes_evidence_list(self) -> None:
        """Verify evidence lists are normalized."""
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            raw_output="""{
                "candidate_events": [{
                    "event_type": "hypothesis.proposed",
                    "entity_type": "hypothesis",
                    "snapshot_ref": "snap1",
                    "payload": {
                        "claim": "Test",
                        "evidence": [{
                            "file_path": "src/app.py",
                            "snapshot_ref": "snap1"
                        }]
                    }
                }]
            }"""
        )
        assert result.success
        evidence = result.candidate_events[0]["payload"]["evidence"][0]
        assert "line_range" in evidence
        assert evidence["range_inferred"] is True

    def test_preserves_valid_line_range(self) -> None:
        """Verify valid line_range is preserved without inference."""
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            raw_output="""{
                "candidate_events": [{
                    "event_type": "observation.proposed",
                    "entity_type": "observation",
                    "snapshot_ref": "snap1",
                    "payload": {
                        "claim": "Test",
                        "provenance": {
                            "source_refs": [{
                                "file_path": "src/app.py",
                                "snapshot_ref": "snap1",
                                "line_range": {"start": 10, "end": 20}
                            }]
                        }
                    }
                }]
            }"""
        )
        assert result.success
        source_ref = result.candidate_events[0]["payload"]["provenance"]["source_refs"][0]
        assert source_ref["line_range"] == {"start": 10, "end": 20}
        assert source_ref["range_inferred"] is False

    def test_missing_line_start_no_longer_breaks_transport(self) -> None:
        """STEP 1 INVARIANT: Missing line_start no longer breaks transport."""
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            raw_output="""{
                "candidate_events": [{
                    "event_type": "observation.proposed",
                    "entity_type": "observation",
                    "snapshot_ref": "snap1",
                    "payload": {
                        "claim": "Test",
                        "provenance": {
                            "source_refs": [{
                                "file_path": "src/app.py",
                                "snapshot_ref": "snap1",
                                "line_range": {"end": 20}
                            }]
                        }
                    }
                }]
            }"""
        )
        # Should NOT fail - this is the STEP 1 invariant
        assert result.success
        source_ref = result.candidate_events[0]["payload"]["provenance"]["source_refs"][0]
        assert source_ref["range_inferred"] is True
        assert source_ref["line_range"]["start"] == DEFAULT_LINE_START

    def test_all_fallback_ranges_marked_inferred(self) -> None:
        """STEP 1 INVARIANT: All fallback ranges are marked range_inferred=true."""
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            raw_output="""{
                "candidate_events": [{
                    "event_type": "observation.proposed",
                    "entity_type": "observation",
                    "snapshot_ref": "snap1",
                    "payload": {
                        "claim": "Test",
                        "provenance": {
                            "source_refs": [
                                {"file_path": "a.py", "snapshot_ref": "s1"},
                                {"file_path": "b.py", "snapshot_ref": "s2", "line_range": {}},
                                {"file_path": "c.py", "snapshot_ref": "s3", "line_range": {"start": -1}}
                            ]
                        }
                    }
                }]
            }"""
        )
        assert result.success
        refs = result.candidate_events[0]["payload"]["provenance"]["source_refs"]
        assert refs[0]["range_inferred"] is True
        assert refs[1]["range_inferred"] is True
        assert refs[2]["range_inferred"] is True

    def test_no_silent_fake_precision(self) -> None:
        """STEP 1 INVARIANT: No silent fake precision - warnings are explicit."""
        normalizer = OutputNormalizer()
        result = normalizer.normalize(
            raw_output="""{
                "candidate_events": [{
                    "event_type": "observation.proposed",
                    "entity_type": "observation",
                    "snapshot_ref": "snap1",
                    "payload": {
                        "claim": "Test",
                        "provenance": {
                            "source_refs": [{
                                "file_path": "src/app.py",
                                "snapshot_ref": "snap1"
                            }]
                        }
                    }
                }]
            }"""
        )
        assert result.success
        source_ref = result.candidate_events[0]["payload"]["provenance"]["source_refs"][0]
        assert "normalization_warning" in source_ref
        assert source_ref["normalization_warning"] is not None
