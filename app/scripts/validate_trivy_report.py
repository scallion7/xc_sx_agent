"""将 Trivy JSON 报告转换为可定位的 GitHub Actions 安全门禁。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SEVERITY_RANK = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "UNKNOWN": 4,
}


def find_vulnerabilities(report: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for result in report.get("Results", []):
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target") or "unknown")
        for vulnerability in result.get("Vulnerabilities") or []:
            if not isinstance(vulnerability, dict):
                continue
            findings.append(
                {
                    "target": target,
                    "id": str(vulnerability.get("VulnerabilityID") or "UNKNOWN"),
                    "package": str(vulnerability.get("PkgName") or "unknown"),
                    "installed": str(
                        vulnerability.get("InstalledVersion") or "unknown"
                    ),
                    "fixed": str(vulnerability.get("FixedVersion") or "unavailable"),
                    "severity": str(vulnerability.get("Severity") or "UNKNOWN").upper(),
                }
            )
    return sorted(
        findings,
        key=lambda item: (
            SEVERITY_RANK.get(item["severity"], len(SEVERITY_RANK)),
            item["package"],
            item["id"],
        ),
    )


def github_error_annotation(finding: dict[str, str]) -> str:
    title = f"Trivy {finding['severity']}"
    message = (
        f"{finding['id']} affects {finding['package']} "
        f"{finding['installed']} in {finding['target']}; "
        f"upgrade to {finding['fixed']}"
    )
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error file=Dockerfile,line=1,title={title}::{escaped}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    findings = find_vulnerabilities(report)
    if not findings:
        print(json.dumps({"actionable_vulnerabilities": 0, "passed": True}))
        return
    for finding in findings:
        print(github_error_annotation(finding))
    raise SystemExit(f"Trivy 检测到 {len(findings)} 个可修复的高危漏洞")


if __name__ == "__main__":
    main()
