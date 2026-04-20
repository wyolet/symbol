"""Checker metadata — describes what a checker does and how to run it."""

from dataclasses import dataclass

from wyolet.symbol.shared.findings import Severity


@dataclass(frozen=True)
class CheckerInfo:
    """Metadata for a registered checker."""

    name: str
    description: str
    kind: str  # "file" or "project"
    default_severity: Severity = Severity.WARNING
    contributes_to_report: bool = True
    priority: int = 100  # lower runs first
