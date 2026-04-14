"""Findings model — severity-tagged results with summary."""

from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
    SKIP = "skip"         # suppressed entirely — not recorded anywhere
    DEBUG = "debug"       # seen, suppressed by default — skip-list files, noise
    INFO = "info"         # informational, shown with -v
    WARNING = "warning"   # default output threshold
    ERROR = "error"       # real problems
    CRITICAL = "critical" # parse failures, dangerous calls, blocking issues

    @property
    def _order(self) -> int:
        return {"skip": -1, "debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}[self.value]

    def __lt__(self, other: "Severity") -> bool:
        return self._order < other._order

    def __le__(self, other: "Severity") -> bool:
        return self._order <= other._order

    def __gt__(self, other: "Severity") -> bool:
        return self._order > other._order

    def __ge__(self, other: "Severity") -> bool:
        return self._order >= other._order


SEVERITY_STYLE = {
    Severity.SKIP: ("dim", "○"),
    Severity.DEBUG: ("dim", "\u00b7"),
    Severity.INFO: ("blue", "\u00b7"),
    Severity.WARNING: ("yellow", "!"),
    Severity.ERROR: ("red", "\u2717"),
    Severity.CRITICAL: ("bold red", "\u2718"),
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
    def criticals(self) -> int:
        return self.count(Severity.CRITICAL)

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
    def debugs(self) -> int:
        return self.count(Severity.DEBUG)

    @property
    def has_issues(self) -> bool:
        return self.errors > 0 or self.criticals > 0

    @property
    def exit_code(self) -> int:
        return 1 if self.has_issues else 0
