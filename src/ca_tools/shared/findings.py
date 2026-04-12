"""Findings model — severity-tagged results with summary."""

from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    def __lt__(self, other: "Severity") -> bool:
        order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}
        return order[self] < order[other]


SEVERITY_STYLE = {
    Severity.ERROR: ("red", "\u2717"),
    Severity.WARNING: ("yellow", "!"),
    Severity.INFO: ("blue", "\u00b7"),
}


@dataclass
class Finding:
    section: str
    message: str
    detail: str = ""
    severity: Severity = Severity.ERROR
    location: str = ""


@dataclass
class Report:
    """Collects findings across all sections and provides summary."""

    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        section: str,
        message: str,
        detail: str = "",
        severity: Severity = Severity.ERROR,
        location: str = "",
    ) -> None:
        self.findings.append(
            Finding(
                section=section,
                message=message,
                detail=detail,
                severity=severity,
                location=location,
            )
        )

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def errors(self) -> int:
        return self.count(Severity.ERROR)

    @property
    def warnings(self) -> int:
        return self.count(Severity.WARNING)

    @property
    def infos(self) -> int:
        return self.count(Severity.INFO)

    @property
    def has_issues(self) -> bool:
        return self.errors > 0

    @property
    def exit_code(self) -> int:
        return 1 if self.errors > 0 else 0
