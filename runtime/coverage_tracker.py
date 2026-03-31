"""
Coverage Tracker for Audit Regression Detection

Tracks coverage of defect classes and detects regressions when coverage drops.

Defect classes to track:
- auth: Authentication/authorization issues
- injection: SQL injection, command injection, XSS
- config: Configuration problems
- validation: Input validation issues
- secrets: Hardcoded secrets, exposed credentials
- crypto: Cryptographic weaknesses
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Defect class patterns for detection
DEFECT_CLASS_PATTERNS = {
    "auth": [
        "auth", "authentication", "authorization", "login", "session",
        "password", "token", "oauth", "jwt", "access control"
    ],
    "injection": [
        "sql", "injection", "xss", "command injection", "code injection",
        "ldap injection", "nosql injection", "sqli"
    ],
    "config": [
        "config", "configuration", "settings", "environment", "debug",
        "cors", "csp", "security headers"
    ],
    "validation": [
        "validation", "input", "sanitize", "escape", "filter",
        "bound", "range", "format"
    ],
    "secrets": [
        "secret", "password", "api key", "token", "credential",
        "private key", "hardcoded"
    ],
    "crypto": [
        "crypto", "encryption", "hash", "md5", "sha1", "aes",
        "ssl", "tls", "certificate"
    ],
}


@dataclass
class CoverageReport:
    """Coverage report for a single audit."""

    audit_id: str
    total_findings: int
    findings_with_severity: int
    severity_distribution: dict[str, int] = field(default_factory=dict)
    class_coverage: dict[str, int] = field(default_factory=dict)
    missing_classes: list[str] = field(default_factory=list)
    regression_detected: bool = False
    regression_details: list[str] = field(default_factory=list)


def classify_finding(finding: dict[str, Any]) -> set[str]:
    """
    Classify a finding into defect classes based on content.

    Args:
        finding: Finding dict with title, summary, etc.

    Returns:
        Set of detected defect classes
    """
    text = " ".join([
        finding.get("title", ""),
        finding.get("summary", ""),
    ]).lower()

    classes = set()
    for class_name, patterns in DEFECT_CLASS_PATTERNS.items():
        for pattern in patterns:
            if pattern in text:
                classes.add(class_name)
                break

    return classes


def analyze_coverage(
    findings: list[dict[str, Any]],
    audit_id: str,
    baseline_classes: set[str] | None = None,
) -> CoverageReport:
    """
    Analyze coverage of findings.

    Args:
        findings: List of finding dicts
        audit_id: Audit identifier
        baseline_classes: Expected classes (if None, uses all defined classes)

    Returns:
        CoverageReport with coverage analysis
    """
    report = CoverageReport(
        audit_id=audit_id,
        total_findings=len(findings),
        findings_with_severity=0,
        severity_distribution={},
        class_coverage={class_name: 0 for class_name in DEFECT_CLASS_PATTERNS},
    )

    # Analyze each finding
    for finding in findings:
        # Severity tracking
        severity = finding.get("severity")
        if severity:
            report.findings_with_severity += 1
            report.severity_distribution[severity] = (
                report.severity_distribution.get(severity, 0) + 1
            )

        # Class coverage
        classes = classify_finding(finding)
        for cls in classes:
            if cls in report.class_coverage:
                report.class_coverage[cls] += 1

    # Determine expected classes
    if baseline_classes is None:
        baseline_classes = set(DEFECT_CLASS_PATTERNS.keys())

    # Find missing classes
    report.missing_classes = [
        cls for cls in baseline_classes
        if report.class_coverage.get(cls, 0) == 0
    ]

    # Detect regression (no coverage for expected classes)
    if report.missing_classes and baseline_classes:
        report.regression_detected = True
        report.regression_details = [
            f"Missing coverage for: {', '.join(report.missing_classes)}"
        ]

    return report


def compare_audits(
    baseline_report: CoverageReport,
    current_report: CoverageReport,
) -> dict[str, Any]:
    """
    Compare two audits for regression detection.

    Returns:
        Dict with regression analysis
    """
    result = {
        "baseline_audit": baseline_report.audit_id,
        "current_audit": current_report.audit_id,
        "findings_delta": current_report.total_findings - baseline_report.total_findings,
        "severity_coverage_delta": (
            current_report.findings_with_severity - baseline_report.findings_with_severity
        ),
        "class_coverage_delta": {},
        "regression_detected": False,
        "regression_details": [],
    }

    # Compare class coverage
    all_classes = set(baseline_report.class_coverage.keys()) | set(
        current_report.class_coverage.keys()
    )
    for cls in all_classes:
        baseline_count = baseline_report.class_coverage.get(cls, 0)
        current_count = current_report.class_coverage.get(cls, 0)
        delta = current_count - baseline_count
        result["class_coverage_delta"][cls] = delta

        # Regression: class was covered before, now not
        if baseline_count > 0 and current_count == 0:
            result["regression_detected"] = True
            result["regression_details"].append(
                f"REGRESSION: Class '{cls}' coverage dropped from {baseline_count} to 0"
            )

    return result


def generate_coverage_report_from_file(
    report_path: Path,
    baseline_classes: set[str] | None = None,
) -> CoverageReport:
    """
    Generate coverage report from audit report file.

    Args:
        report_path: Path to report JSON file
        baseline_classes: Expected classes

    Returns:
        CoverageReport
    """
    with open(report_path) as f:
        report_data = json.load(f)

    findings = report_data.get("findings", [])
    audit_id = report_data.get("source_audit_id", "unknown")

    return analyze_coverage(findings, audit_id, baseline_classes)
