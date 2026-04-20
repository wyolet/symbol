"""Analyze CLI — per-file deep analysis with Rich renderables."""

import json
from pathlib import Path

from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from wyolet.symbol.shared.analyzer import FileAnalysis, analyze_all, analyze_file

console = Console()


def analyze_dump(path: str, output: str, cache=None) -> None:
    """Dump full per-file analysis of all Python files to JSON."""
    project_root = Path(path)
    results = analyze_all(project_root, cache=cache)
    abs_root = str(project_root.resolve())

    data = {
        "project": project_root.name,
        "files": [_file_to_dict(r) for r in results],
    }

    out_path = Path(output)
    out_path.write_text(json.dumps(data, indent=2))
    console.print(f"  [green]\u2713[/green] {len(results)} files analyzed \u2192 [bold]{output}[/bold]")


def _file_to_dict(r: FileAnalysis) -> dict:
    return {
        "path": r.path,
        "lines": r.lines,
        "sloc": r.sloc,
        "classes": r.classes,
        "functions": r.functions,
        "typed_pct": round(r.typed_pct, 1),
        "total_complexity": r.total_complexity,
        "max_depth": r.max_depth,
        "blast_radius": {
            "direct": r.direct_importers,
            "transitive": r.transitive_importers,
            "total": r.direct_importers + r.transitive_importers,
        },
        "exports": [
            {
                "name": e.name, "kind": e.kind, "line": e.line, "lines": e.lines,
                "complexity": e.complexity, "max_depth": e.max_depth,
                "internal_refs": e.internal_refs, "used_by": e.used_by,
                "methods": [
                    {"name": m.name, "line": m.line, "lines": m.lines, "complexity": m.complexity, "max_depth": m.max_depth, "is_async": m.is_async}
                    for m in e.methods
                ] if e.methods else [],
            }
            for e in r.exports
        ],
        "imports": [
            {"name": i.name, "source_module": i.source_module, "source_file": i.source_file, "scope": i.scope}
            for i in r.imports
        ],
    }


def analyze_cmd(path: str, target: str, format: str = "rich") -> None:
    """Run per-file analysis."""
    project_root = Path(path)

    result = analyze_file(project_root, target)

    if result is None:
        console.print(f"\n  [red]File not found:[/red] [bold]{target}[/bold]\n")
        return

    if format == "json":
        _print_json(result)
        return

    renderables = [
        Text(),  # spacer
        _render_header(result),
        Text(),
        _render_exports(result),
        Text(),
        _render_blast(result),
    ]

    # Only show imports section if there are hidden imports worth flagging
    hidden = _render_hidden_imports(result)
    if hidden:
        renderables.append(Text())
        renderables.append(hidden)

    renderables.append(Text())
    renderables.append(_render_caveat())
    renderables.append(Text())

    console.print(Group(*renderables))


# ── Renderables ──────────────────────────────────────────────────────


def _render_header(r: FileAnalysis) -> Group:
    """File overview panel."""
    panel = Panel(
        Text(f"  symbol analyze — {r.path}", style="bold"),
        style="dim",
        expand=False,
    )

    typed_style = "green" if r.typed_pct >= 80 else "cyan" if r.typed_pct >= 50 else "yellow" if r.typed_pct > 0 else "red"
    cc_style = "green" if r.total_complexity < 15 else "yellow" if r.total_complexity < 30 else "red"
    depth_style = "green" if r.max_depth <= 3 else "yellow" if r.max_depth <= 5 else "red"

    metrics = Text.assemble(
        (f"{r.sloc:,}", "bold"), " sloc  ",
        (f"{r.lines:,}", "dim"), " lines  ",
        (f"{r.classes}", "bold"), " classes  ",
        (f"{r.functions}", "bold"), " functions  ",
        (f"{r.typed_pct:.0f}%", typed_style), " typed  ",
        ("cc:", "dim"), (f"{r.total_complexity}", cc_style), "  ",
        ("depth:", "dim"), (f"{r.max_depth}", depth_style),
    )

    return Group(panel, Padding(metrics, (0, 0, 0, 2)))


def _render_exports(r: FileAnalysis) -> Group:
    """Exports table with per-name blast radius."""
    header = Text("\U0001f4e4 EXPORTS", style="bold")

    if not r.exports:
        return Group(
            Padding(header, (0, 0, 0, 2)),
            Padding(Text("(no public exports)", style="dim"), (0, 0, 0, 4)),
        )

    desc = Text("Names defined in this file and who imports them", style="dim")

    # Filter: hide variables with 0 importers — they're internal to the file
    visible = [e for e in r.exports if e.used_by or e.kind != "variable"]
    sorted_exports = sorted(visible, key=lambda e: (-len(e.used_by), e.name))

    table = Table(box=None, padding=(0, 1, 0, 0), show_edge=False, show_header=True)
    table.add_column("Kind", style="dim", min_width=8)
    table.add_column("Name", style="bold", min_width=30, no_wrap=True)
    table.add_column("CC", justify="right", min_width=6)
    table.add_column("Depth", justify="right", min_width=5)
    table.add_column("Usage (int/ext)", min_width=7)

    for exp in sorted_exports:
        # Usage column: internal refs / external importers
        ext = len(exp.used_by)
        intr = exp.internal_refs
        if ext == 0 and intr == 0:
            usage = Text("unused", style="red")
        else:
            ext_s = "green" if ext > 0 else "dim"
            usage = Text.assemble((f"{intr}", "dim"), ("/", "dim"), (f"{ext}", ext_s))

        # CC column
        if exp.kind == "variable" or exp.complexity == 0:
            cc_text = Text("")
        elif exp.kind == "class" and exp.methods:
            max_cc = max(m.complexity for m in exp.methods)
            cc_s = "green" if max_cc <= 5 else "yellow" if max_cc <= 10 else "red"
            cc_text = Text.assemble((f"{max_cc}", cc_s), (f"/{exp.complexity}", "dim"))
        else:
            cc_s = "green" if exp.complexity <= 5 else "yellow" if exp.complexity <= 10 else "red"
            cc_text = Text(f"{exp.complexity}", style=cc_s)

        # Depth column
        if exp.kind == "variable" or exp.max_depth == 0:
            depth_text = Text("")
        else:
            d_s = "green" if exp.max_depth <= 3 else "yellow" if exp.max_depth <= 5 else "red"
            depth_text = Text(f"{exp.max_depth}", style=d_s)

        table.add_row(exp.kind, exp.name, cc_text, depth_text, usage)

        # Expand concerning methods for classes
        if exp.kind == "class" and exp.methods:
            concerning = [m for m in exp.methods if m.complexity > 5 or m.max_depth > 3]
            ok_count = len(exp.methods) - len(concerning)
            for m in concerning[:5]:
                m_cc_s = "yellow" if m.complexity <= 10 else "red"
                m_d_s = "yellow" if m.max_depth <= 5 else "red"
                prefix = "\u2931 async " if m.is_async else "\u2931 "
                name_text = Text()
                name_text.append(prefix, style="dim")
                name_text.append(f"{m.name}()", style="dim")
                table.add_row("", name_text, Text(f"{m.complexity}", style=m_cc_s), Text(f"{m.max_depth}", style=m_d_s), Text(""))
            if len(concerning) > 5:
                table.add_row("", Text(f"  ...{len(concerning) - 5} more", style="dim"), "", "", "")
            if ok_count > 0 and concerning:
                table.add_row("", Text(f"  {ok_count} methods ok", style="dim"), "", "", "")

    parts: list = [
        Padding(header, (0, 0, 0, 2)),
        Padding(desc, (0, 0, 0, 4)),
        Padding(Text(), (0, 0, 0, 0)),
        Padding(table, (0, 0, 0, 4)),
    ]

    # Importers listed below the table — full width for paths
    used_exports = [e for e in sorted_exports if e.used_by]
    if used_exports:
        parts.append(Padding(Text(), (0, 0, 0, 0)))
        for exp in used_exports:
            shown = exp.used_by[:5]
            more = f", +{len(exp.used_by) - 5} more" if len(exp.used_by) > 5 else ""
            importers = ", ".join(shown) + more
            parts.append(Padding(
                Text.assemble((exp.name, "bold"), (" \u2190 ", "dim"), (importers, "dim")),
                (0, 0, 0, 4),
            ))

    return Group(*parts)


def _render_hidden_imports(r: FileAnalysis) -> Group | None:
    """Show only hidden imports — deferred (in function bodies) and TYPE_CHECKING."""
    deferred = [i for i in r.imports if i.scope == "deferred"]
    tc = [i for i in r.imports if i.scope == "type_checking"]

    if not deferred and not tc:
        return None

    header = Text("\U0001f50d HIDDEN IMPORTS", style="bold")
    parts: list = [Padding(header, (0, 0, 0, 2))]

    if deferred:
        parts.append(Padding(
            Text.assemble(("Deferred ", "yellow bold"), ("— imports inside function bodies, may hide circular deps", "dim")),
            (1, 0, 0, 4),
        ))
        for imp in deferred:
            source = imp.source_file or imp.source_module
            parts.append(Padding(
                Text.assemble(("! ", "yellow"), (imp.name, "bold"), (" from ", "dim"), (source, "cyan")),
                (0, 0, 0, 4),
            ))

    if tc:
        parts.append(Padding(
            Text.assemble((f"{len(tc)} TYPE_CHECKING imports", "dim"), (" — not loaded at runtime", "dim italic")),
            (1, 0, 0, 4),
        ))

    return Group(*parts)


def _render_blast(r: FileAnalysis) -> Group:
    """Blast radius summary."""
    header = Text("\U0001f4a5 BLAST RADIUS", style="bold")
    total = r.direct_importers + r.transitive_importers

    if total == 0:
        return Group(
            Padding(header, (0, 0, 0, 2)),
            Padding(Text("No other files depend on this module", style="dim"), (0, 0, 0, 4)),
        )

    blast_text = Text.assemble(
        (f"{total}", "bold red"), " files affected  ",
        (f"{r.direct_importers}", "bold"), " direct  ",
        (f"{r.transitive_importers}", "dim"), " transitive",
    )

    return Group(
        Padding(header, (0, 0, 0, 2)),
        Padding(blast_text, (0, 0, 0, 4)),
    )


def _render_caveat() -> Padding:
    """Static analysis caveat."""
    text = Text(
        "static analysis only — dynamic access (getattr, dict dispatch, plugin registries) not tracked",
        style="dim italic",
    )
    return Padding(text, (0, 0, 0, 2))


# ── JSON ─────────────────────────────────────────────────────────────


def _print_json(r: FileAnalysis) -> None:
    print(json.dumps(_file_to_dict(r), indent=2))
