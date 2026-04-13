"""Audit CLI — detect stack, entry points, orphans, side effects."""

import fnmatch
import json
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

import ca_tools.frameworks  # noqa: F401 — registers framework hooks
from ca_tools.loc.linguist import Linguist
from ca_tools.shared.ast_cache import ASTCache
from ca_tools.shared.findings import SEVERITY_STYLE, Report, Severity
from ca_tools.shared.project_config import load_project_config
from ca_tools.shared.spec import Spec, load_spec

from .codestructure import CodeStructure, detect_code_structure
from .entrypoints import EntryPoint, detect_entrypoints
from .sideeffects import SideEffect, detect_sideeffects
from .stack import detect_deps, detect_stack
from .todos import TodoItem, detect_todos
from .unused_deps import detect_unused_deps

console = Console()

# Consistent indentation
I1 = "  "
I2 = "    "

# Categories that represent the core architecture
_PRIMARY_CATEGORIES = {"web", "orm", "database_driver", "task_queue", "llm", "migration"}


def _collapse_packages(packages: list[str]) -> list[str]:
    """Collapse package variants into their base: langchain-openai → langchain."""
    bases: dict[str, str] = {}
    for pkg in packages:
        base = pkg.split("-")[0]
        if base not in bases or len(pkg) < len(bases[base]):
            bases[base] = pkg
    return list(bases.values())


# ── Main command ─────────────────────────────────────────────────────


def audit_cmd(
    path: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    verbose: bool = False,
    format: str = "rich",
) -> None:
    """Run the full audit."""
    project_root = Path(path)
    project_name = project_root.name
    spec = load_spec()
    config = load_project_config(project_root)
    report = Report()

    inc = include or config.include or None
    exc = exclude or config.exclude or None

    # Parse all files once, share ASTs across detectors
    cache = ASTCache(project_root, inc, exc)

    # Gather data
    stack = detect_stack(project_root, spec)
    entrypoints = detect_entrypoints(project_root, inc, exc, cache)
    deps = detect_deps(project_root)
    unused_raw = detect_unused_deps(project_root, deps, spec, inc, exc, cache)
    code_structure = detect_code_structure(project_root, cache)
    todos = detect_todos(project_root, cache)

    # Side effects with ignore filtering
    sideeffects_raw = detect_sideeffects(project_root, spec, inc, exc, cache)
    if config.ignore_side_effects:
        sideeffects = [
            se for se in sideeffects_raw
            if not any(fnmatch.fnmatch(se.call_text, pat) for pat in config.ignore_side_effects)
        ]
    else:
        sideeffects = sideeffects_raw

    # Unused deps with ignore filtering
    if config.ignore_deps:
        unused = [d for d in unused_raw if d not in config.ignore_deps]
    else:
        unused = unused_raw

    # Free AST cache
    cache.clear()

    # Linguist for project shape (cheap, ~300ms)
    linguist = Linguist()
    loc_stats = linguist.detect_directory(str(project_root))

    # Populate report
    sev = config.severity_side_effects
    for se in sideeffects:
        rel = str(se.filepath.relative_to(project_root))
        report.add("side_effects", se.call_text, "", sev, f"{rel}:{se.lineno}")

    sev = config.severity_unused_deps
    for dep in unused:
        report.add("unused_deps", dep, "in pyproject.toml, 0 imports found", sev, dep)

    if format == "json":
        _print_json(project_root, loc_stats, code_structure, stack, entrypoints, sideeffects, todos, deps, unused, report)
        sys.exit(report.exit_code)
        return

    # ── Rich output ──────────────────────────────────────────────────

    console.print()
    console.print(Panel(Text(f"{I1}ca audit \u2014 {project_name}/", style="bold"), style="dim", expand=False))

    # Project shape
    _print_shape(loc_stats, code_structure)

    # Type coverage
    _print_type_coverage(code_structure)

    # Stack
    _print_stack(stack, spec, verbose)

    # Entry points
    _print_entrypoints(entrypoints, project_root, verbose)

    # Side effects
    _print_sideeffects(sideeffects, project_root, config.severity_side_effects, verbose)

    # TODOs
    _print_todos(todos, project_root)

    # Deps summary (nudge to ca deps for full detail)
    _print_deps_summary(deps, unused)

    _print_summary(report)
    sys.exit(report.exit_code)


# ── Section printers ─────────────────────────────────────────────────


def _print_shape(loc_stats: list[dict], cs: CodeStructure) -> None:
    console.print()

    # Find primary programming language
    code_langs = [s for s in loc_stats if s.get("type") == "programming"]
    code_langs.sort(key=lambda s: s["sloc"], reverse=True)
    total_sloc = sum(s["sloc"] for s in loc_stats)
    total_files = sum(s["files"] for s in loc_stats)
    total_size = sum(s["size"] for s in loc_stats)

    primary = code_langs[0] if code_langs else None
    primary_pct = f"{primary['percentage_lines']:.0f}%" if primary and primary.get("percentage_lines") else ""
    primary_name = primary["name"] if primary else "Unknown"
    primary_color = primary.get("color", "#888") if primary else "#888"

    console.print(Text(f"{I1}\U0001f4d0 PROJECT SHAPE", style="bold"))
    console.print()

    # Line 1: size metrics
    console.print(
        f"{I2}[bold]{total_sloc:,}[/bold] [dim]sloc[/dim]  "
        f"[bold]{total_files:,}[/bold] [dim]files[/dim]  "
        f"[bold]{total_size / 1024:.0f}K[/bold] [dim]size[/dim]  "
        f"[{primary_color}]\u25cf[/{primary_color}] [bold]{primary_name}[/bold] [dim]{primary_pct}[/dim]"
    )

    # Line 2: code structure
    console.print(
        f"{I2}[bold]{cs.functions}[/bold] [dim]functions[/dim]  "
        f"[bold]{cs.methods}[/bold] [dim]methods[/dim]  "
        f"[bold]{cs.classes}[/bold] [dim]classes[/dim]  "
        f"[bold]{cs.avg_function_lines}[/bold] [dim]avg lines/fn[/dim]"
    )



def _pct_style(pct: float) -> str:
    if pct == 0:
        return "red"
    if pct < 50:
        return "yellow"
    if pct < 80:
        return "cyan"
    return "green"


def _type_line(label: str, typed: int, total: int, pct: float) -> str:
    style = _pct_style(pct)
    return f"[{style}]{pct:.0f}%[/{style}] [dim]{label}[/dim]  [dim]({typed}/{total})[/dim]"


def _print_type_coverage(cs: CodeStructure) -> None:
    if cs.total_callables == 0:
        return

    # Overall ratio: functions + arguments + class attributes
    total_typed = cs.typed_functions + cs.typed_args + cs.typed_attrs
    total_checkable = cs.total_callables + cs.total_args + cs.total_attrs
    overall_pct = (total_typed / total_checkable * 100) if total_checkable else 0
    overall_style = _pct_style(overall_pct)

    console.print()
    console.print(Text(f"{I1}\U0001f3f7\ufe0f  TYPE COVERAGE", style="bold"))
    console.print()

    # Overall + breakdown
    parts = [f"[{overall_style} bold]{overall_pct:.0f}%[/{overall_style} bold] [dim]overall[/dim]"]
    parts.append(f"[{_pct_style(cs.type_coverage_pct)}]{cs.type_coverage_pct:.0f}%[/{_pct_style(cs.type_coverage_pct)}] [dim]functions[/dim]")
    parts.append(f"[{_pct_style(cs.arg_coverage_pct)}]{cs.arg_coverage_pct:.0f}%[/{_pct_style(cs.arg_coverage_pct)}] [dim]args[/dim]")
    if cs.total_attrs > 0:
        parts.append(f"[{_pct_style(cs.attr_coverage_pct)}]{cs.attr_coverage_pct:.0f}%[/{_pct_style(cs.attr_coverage_pct)}] [dim]attrs[/dim]")
    console.print(f"{I2}{'  '.join(parts)}")

    # Vars — excluded from overall, shown for visibility
    if cs.total_vars > 0:
        var_pct = cs.typed_vars / cs.total_vars * 100
        console.print(f"{I2}[dim]{cs.typed_vars}/{cs.total_vars} variables typed (not in overall — inferred by type checkers)[/dim]")


def _print_stack(stack: dict[str, list[str]], spec: Spec, verbose: bool) -> None:
    console.print()
    console.print(Text(f"{I1}\U0001f4e6 STACK", style="bold"))
    console.print()

    if not stack:
        console.print(f"{I2}[dim](no recognized dependencies)[/dim]")
        return

    if verbose:
        order = list(spec.categories.keys())
        for cat in order:
            if cat not in stack:
                continue
            label = spec.categories.get(cat, cat)
            pkgs = ", ".join(stack[cat])
            console.print(f"{I2}[bold]{label + ':':<20s}[/bold] [cyan]{pkgs}[/cyan]")
        for cat in sorted(stack):
            if cat not in spec.categories:
                pkgs = ", ".join(stack[cat])
                console.print(f"{I2}[bold]{cat + ':':<20s}[/bold] [cyan]{pkgs}[/cyan]")
    else:
        order = list(spec.categories.keys())
        shown = []
        others: list[str] = []

        for cat in order:
            if cat not in stack:
                continue
            label = spec.categories.get(cat, cat)
            pkgs = _collapse_packages(stack[cat])
            if cat in _PRIMARY_CATEGORIES:
                shown.append((label, pkgs))
            else:
                others.extend(pkgs)

        for cat in sorted(stack):
            if cat not in spec.categories:
                others.extend(_collapse_packages(stack[cat]))

        for label, pkgs in shown:
            console.print(f"{I2}[bold]{label + ':':<20s}[/bold] [cyan]{', '.join(pkgs)}[/cyan]")

        if others:
            console.print(f"{I2}[dim]also:[/dim] [dim]{', '.join(sorted(set(others)))}[/dim]")


def _print_entrypoints(entrypoints: list[EntryPoint], project_root: Path, verbose: bool) -> None:
    console.print()
    console.print(Text(f"{I1}\U0001f680 ENTRY POINTS ({len(entrypoints)})", style="bold blue"))
    console.print()

    if not entrypoints:
        console.print(f"{I2}[dim](none found)[/dim]")
        return

    if verbose:
        for ep in entrypoints:
            rel = ep.filepath.relative_to(project_root)
            loc = f"{rel}:{ep.lineno}"
            guard = " [dim]\u2190 if __name__[/dim]" if ep.in_main_guard else ""
            console.print(f"{I2}[green]\u2713[/green] [bold]{loc:<30s}[/bold] {ep.description}{guard}")
    else:
        by_file: dict[Path, list[EntryPoint]] = defaultdict(list)
        for ep in entrypoints:
            by_file[ep.filepath].append(ep)
        for filepath, eps in by_file.items():
            rel = filepath.relative_to(project_root)
            calls = ", ".join(ep.description for ep in eps)
            console.print(f"{I2}[green]\u2713[/green] [bold]{str(rel):<30s}[/bold] {calls}")


def _print_sideeffects(
    sideeffects: list[SideEffect], project_root: Path, sev: Severity, verbose: bool
) -> None:
    console.print()
    if not sideeffects:
        console.print(Text(f"{I1}\u2705 SIDE EFFECTS (0)", style="bold green"))
        console.print()
        console.print(f"{I2}[green]No module-level side effects detected[/green]")
        return

    style_map = {Severity.ERROR: "bold red", Severity.WARNING: "bold yellow", Severity.INFO: "bold blue"}
    icon_map = {Severity.ERROR: "\U0001f534", Severity.WARNING: "\u26a0\ufe0f ", Severity.INFO: "\u2139\ufe0f "}
    console.print(Text(f"{I1}{icon_map[sev]} SIDE EFFECTS ({len(sideeffects)})", style=style_map[sev]))
    console.print(f"{I2}[dim]Bare function calls at module level — runs on import[/dim]")
    console.print()

    cap = 10
    if verbose:
        shown = sideeffects[:cap]
        for se in shown:
            rel = se.filepath.relative_to(project_root)
            loc = f"{rel}:{se.lineno}"
            s, icon = SEVERITY_STYLE[sev]
            console.print(f"{I2}[{s}]{icon}[/{s}] [bold]{loc:<30s}[/bold] [yellow]{se.call_text}[/yellow]")
        if len(sideeffects) > cap:
            console.print(f"{I2}[dim]... and {len(sideeffects) - cap} more[/dim]")
    else:
        by_file: dict[str, list[str]] = defaultdict(list)
        for se in sideeffects:
            rel = str(se.filepath.relative_to(project_root))
            by_file[rel].append(se.call_text)
        items = list(by_file.items())
        for filepath, calls in items[:cap]:
            s, icon = SEVERITY_STYLE[sev]
            call_summary = ", ".join(calls[:3])
            extra = f" +{len(calls) - 3} more" if len(calls) > 3 else ""
            console.print(
                f"{I2}[{s}]{icon}[/{s}] [bold]{filepath:<30s}[/bold] [yellow]{call_summary}{extra}[/yellow]"
            )
        if len(items) > cap:
            console.print(f"{I2}[dim]... and {len(items) - cap} more files[/dim]")


def _print_todos(todos: list[TodoItem], project_root: Path) -> None:
    if not todos:
        return

    console.print()
    # Count by tag
    tag_counts: dict[str, int] = defaultdict(int)
    for t in todos:
        tag_counts[t.tag] += 1

    tag_summary = "  ".join(f"[dim]{tag}:{count}[/dim]" for tag, count in sorted(tag_counts.items()))
    console.print(Text(f"{I1}\U0001f4cc TODO/FIXME ({len(todos)})", style="bold yellow"))
    console.print(f"{I2}{tag_summary}")
    console.print()

    for item in todos[:10]:
        rel = item.filepath.relative_to(project_root)
        loc = f"{rel}:{item.line}"
        tag_color = "red" if item.tag == "FIXME" else "yellow"
        console.print(f"{I2}[{tag_color}]{item.tag}[/{tag_color}]  [bold]{loc:<40s}[/bold] [dim]{item.text}[/dim]")
    if len(todos) > 10:
        console.print(f"{I2}[dim]... and {len(todos) - 10} more[/dim]")


def _print_deps_summary(deps: list[str], unused: list[str]) -> None:
    console.print()
    if not deps:
        return

    parts = [f"[bold]{len(deps)}[/bold] [dim]deps declared[/dim]"]
    if unused:
        parts.append(f"[red]{len(unused)} unused[/red] [dim]\u2014 run[/dim] [bold]ca deps[/bold] [dim]for details[/dim]")
    else:
        parts.append("[green]all imported[/green]")
    console.print(f"{I1}{'  '.join(parts)}")


def _print_summary(report: Report) -> None:
    console.print()
    parts: list[str] = []
    if report.errors:
        parts.append(f"[bold red]{report.errors} error{'s' if report.errors != 1 else ''}[/bold red]")
    if report.warnings:
        parts.append(f"[bold yellow]{report.warnings} warning{'s' if report.warnings != 1 else ''}[/bold yellow]")

    if not parts:
        console.print(Panel("[bold green]\u2705 No issues found[/bold green]", style="green", expand=False))
    else:
        console.print(Panel("  ".join(parts), style="red" if report.errors else "yellow", expand=False))
    console.print()


# ── JSON output ──────────────────────────────────────────────────────


def _print_json(
    project_root: Path,
    loc_stats: list[dict],
    cs: CodeStructure,
    stack: dict,
    entrypoints: list[EntryPoint],
    sideeffects: list[SideEffect],
    todos: list[TodoItem],
    deps: list[str],
    unused: list[str],
    report: Report,
) -> None:
    data = {
        "shape": {
            "sloc": sum(s["sloc"] for s in loc_stats),
            "loc": sum(s["loc"] for s in loc_stats),
            "files": sum(s["files"] for s in loc_stats),
            "size": sum(s["size"] for s in loc_stats),
            "functions": cs.functions,
            "methods": cs.methods,
            "classes": cs.classes,
            "avg_function_lines": cs.avg_function_lines,
            "type_coverage_pct": round(cs.type_coverage_pct, 1),
            "typed_functions": cs.typed_functions,
            "total_callables": cs.total_callables,
            "arg_coverage_pct": round(cs.arg_coverage_pct, 1),
            "typed_args": cs.typed_args,
            "total_args": cs.total_args,
            "attr_coverage_pct": round(cs.attr_coverage_pct, 1),
            "typed_attrs": cs.typed_attrs,
            "total_attrs": cs.total_attrs,
        },
        "stack": {cat: list(pkgs) for cat, pkgs in stack.items()},
        "entrypoints": [
            {
                "file": str(ep.filepath.relative_to(project_root)),
                "lineno": ep.lineno,
                "description": ep.description,
            }
            for ep in entrypoints
        ],
        "sideeffects": [
            {
                "file": str(se.filepath.relative_to(project_root)),
                "lineno": se.lineno,
                "call_text": se.call_text,
            }
            for se in sideeffects
        ],
        "todos": [
            {
                "file": str(t.filepath.relative_to(project_root)),
                "line": t.line,
                "tag": t.tag,
                "text": t.text,
            }
            for t in todos
        ],
        "deps": {
            "declared": len(deps),
            "unused": unused,
        },
        "report": {
            "errors": report.errors,
            "warnings": report.warnings,
        },
    }
    print(json.dumps(data, indent=2))
