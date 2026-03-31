"""
Tests for Coverage Tracker (regression detection)
"""

import pytest

from runtime.coverage_tracker import (
    DEFECT_CLASS_PATTERNS,
    CoverageReport,
    analyze_coverage,
    classify_finding,
    compare_audits,
)


class TestClassifyFinding:
    """Tests for finding classification into defect classes."""

    def test_auth_classification(self):
        """Auth-related findings are classified correctly."""
        finding = {
            "title": "Authentication bypass vulnerability",
            "summary": "Users can bypass login via OAuth token manipulation"
        }
        classes = classify_finding(finding)
        assert "auth" in classes

    def test_injection_classification(self):
        """Injection-related findings are classified correctly."""
        finding = {
            "title": "SQL Injection in user search",
            "summary": "Unsanitized input allows SQL injection"
        }
        classes = classify_finding(finding)
        assert "injection" in classes

    def test_config_classification(self):
        """Config-related findings are classified correctly."""
        finding = {
            "title": "Insecure CORS configuration",
            "summary": "CORS allows all origins"
        }
        classes = classify_finding(finding)
        assert "config" in classes

    def test_secrets_classification(self):
        """Secrets-related findings are classified correctly."""
        finding = {
            "title": "Hardcoded API key in source code",
            "summary": "Secret exposed in config.py"
        }
        classes = classify_finding(finding)
        assert "secrets" in classes

    def test_multiple_classes(self):
        """Findings can have multiple classes."""
        finding = {
            "title": "SQL injection in authentication endpoint",
            "summary": "Auth bypass via SQL injection"
        }
        classes = classify_finding(finding)
        assert "auth" in classes
        assert "injection" in classes

    def test_no_matching_class(self):
        """Findings without matching patterns return empty set."""
        finding = {
            "title": "Missing documentation",
            "summary": "README is empty"
        }
        classes = classify_finding(finding)
        assert classes == set()


class TestAnalyzeCoverage:
    """Tests for coverage analysis."""

    def test_empty_findings(self):
        """Empty findings list produces zero coverage."""
        report = analyze_coverage([], "test_audit")
        assert report.total_findings == 0
        assert all(count == 0 for count in report.class_coverage.values())
        assert report.regression_detected is True  # No coverage for expected classes

    def test_single_finding_coverage(self):
        """Single finding updates coverage correctly."""
        findings = [
            {
                "title": "SQL Injection",
                "summary": "Injection vulnerability",
                "severity": "high"
            }
        ]
        report = analyze_coverage(findings, "test_audit")
        assert report.total_findings == 1
        assert report.class_coverage["injection"] == 1
        assert report.findings_with_severity == 1
        assert report.severity_distribution.get("high") == 1

    def test_severity_distribution(self):
        """Severity distribution is tracked correctly."""
        findings = [
            {"title": "Critical auth bypass", "summary": "...", "severity": "critical"},
            {"title": "High injection", "summary": "...", "severity": "high"},
            {"title": "High xss", "summary": "...", "severity": "high"},
            {"title": "Medium config", "summary": "...", "severity": "medium"},
        ]
        report = analyze_coverage(findings, "test_audit")
        assert report.severity_distribution == {
            "critical": 1,
            "high": 2,
            "medium": 1
        }

    def test_missing_class_detection(self):
        """Missing classes are detected."""
        findings = [
            {"title": "SQL Injection", "summary": "...", "severity": "high"}
        ]
        # Only injection covered, auth/config/etc are missing
        report = analyze_coverage(findings, "test_audit")
        assert "auth" in report.missing_classes
        assert "config" in report.missing_classes
        assert "injection" not in report.missing_classes

    def test_baseline_classes_override(self):
        """Custom baseline classes can be provided."""
        findings = [
            {"title": "SQL Injection", "summary": "...", "severity": "high"}
        ]
        # Only check auth coverage
        report = analyze_coverage(findings, "test_audit", baseline_classes={"auth"})
        assert report.regression_detected is True
        assert report.missing_classes == ["auth"]


class TestCompareAudits:
    """Tests for audit comparison and regression detection."""

    def test_no_regression_when_improved(self):
        """No regression when coverage improves."""
        baseline = CoverageReport(
            audit_id="audit_1",
            total_findings=5,
            findings_with_severity=5,
            class_coverage={"auth": 2, "injection": 1, "config": 1, "validation": 1, "secrets": 0, "crypto": 0}
        )
        current = CoverageReport(
            audit_id="audit_2",
            total_findings=8,
            findings_with_severity=8,
            class_coverage={"auth": 3, "injection": 2, "config": 1, "validation": 1, "secrets": 1, "crypto": 0}
        )
        result = compare_audits(baseline, current)
        assert result["regression_detected"] is False
        assert result["findings_delta"] == 3

    def test_regression_when_class_drops_to_zero(self):
        """Regression detected when a class drops from >0 to 0."""
        baseline = CoverageReport(
            audit_id="audit_1",
            total_findings=5,
            findings_with_severity=5,
            class_coverage={"auth": 2, "injection": 1, "config": 1, "validation": 1, "secrets": 0, "crypto": 0}
        )
        current = CoverageReport(
            audit_id="audit_2",
            total_findings=3,
            findings_with_severity=3,
            class_coverage={"auth": 2, "injection": 0, "config": 0, "validation": 1, "secrets": 0, "crypto": 0}
        )
        result = compare_audits(baseline, current)
        assert result["regression_detected"] is True
        assert any("injection" in detail for detail in result["regression_details"])
        assert any("config" in detail for detail in result["regression_details"])

    def test_findings_delta_calculation(self):
        """Findings delta is calculated correctly."""
        baseline = CoverageReport(
            audit_id="audit_1",
            total_findings=10,
            findings_with_severity=10,
            class_coverage={}
        )
        current = CoverageReport(
            audit_id="audit_2",
            total_findings=5,
            findings_with_severity=5,
            class_coverage={}
        )
        result = compare_audits(baseline, current)
        assert result["findings_delta"] == -5
