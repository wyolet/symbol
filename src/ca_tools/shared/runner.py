"""Checker runner — execute all registered checkers and collect results."""

from typing import Any

from ca_tools.shared.context import AnalysisContext
from ca_tools.shared.registry import get_all
from ca_tools.shared.findings import Report


def run_checkers(ctx: AnalysisContext, report: Report) -> dict[str, Any]:
    """Run all registered, non-disabled checkers.

    Returns raw checker output keyed by name — each checker's own data shape.
    Populates the report via each checker's to_findings view.
    """
    results: dict[str, Any] = {}

    for entry in get_all():
        info = entry.info
        if info.name in ctx.resolved.disabled_checkers:
            continue

        if info.kind == "project":
            items = entry.detect(ctx)
        elif info.kind == "file":
            items = []
            for filepath in ctx.cache.files:
                tree = ctx.cache.get_ast(filepath)
                file_items = entry.detect(ctx, filepath, tree)
                items.extend(file_items)
        else:
            raise ValueError(f"Unknown checker kind {info.kind!r} for {info.name!r}")

        results[info.name] = items
        ctx.checker_results[info.name] = items  # available to subsequent checkers

        # Populate report via to_findings view
        if info.contributes_to_report and entry.to_findings and items:
            for finding in entry.to_findings(items, ctx):
                report.add(
                    finding.section,
                    finding.message,
                    finding.detail,
                    finding.severity,
                    finding.location,
                )

    return results
