//! Human and JSON output of the Python CLI (commands/*.py), reproduced as
//! plain text the way Rich prints when stdout is not a terminal: markup
//! dropped, lines word-wrapped to the console width, panels/tables drawn
//! with the same box characters.

pub mod agent;

use serde_json::{json, Value};

use crate::pyjson::{dumps, dumps_indent};
use crate::pystr::repr;
use agent::py;

// ---------------------------------------------------------- console

/// Rich's console width off a terminal: `$COLUMNS`, else 80.
pub fn width() -> usize {
    std::env::var("COLUMNS")
        .ok()
        .and_then(|c| c.parse().ok())
        .filter(|&w: &usize| w > 0)
        .unwrap_or(80)
}

fn cells(s: &str) -> usize {
    s.chars().count()
}

/// Rich markup → plain text: `[style]` / `[/style]` tags vanish (anything
/// starting `[a-z#/@` up to `]`), `\[` escapes a literal bracket.
pub fn plain(markup: &str) -> String {
    static TAG: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"(\\*)\[([a-z#/@][^\[]*?)\]").unwrap());
    let mut out = String::new();
    let mut pos = 0;
    for m in TAG.captures_iter(markup) {
        let whole = m.get(0).unwrap();
        out.push_str(&markup[pos..whole.start()]);
        let escapes = m[1].len();
        out.push_str(&"\\".repeat(escapes / 2));
        if escapes % 2 == 1 {
            out.push_str(&whole.as_str()[escapes..]);
        }
        pos = whole.end();
    }
    out.push_str(&markup[pos..]);
    out
}

/// Rich's `divide_line` + fold: words carry their trailing whitespace, a
/// word that doesn't fit starts a new line, an over-long word is chopped.
pub fn wrap(text: &str, width: usize) -> Vec<String> {
    divide(text, width, true)
}

fn divide(text: &str, width: usize, fold: bool) -> Vec<String> {
    static WORD: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"\s*\S+\s*").unwrap());
    let chars: Vec<char> = text.chars().collect();
    let byte_to_char = |b: usize| text[..b].chars().count();
    let mut breaks: Vec<usize> = vec![];
    let mut offset = 0usize;
    for m in WORD.find_iter(text) {
        let mut start = byte_to_char(m.start());
        let word = m.as_str();
        let word_len = cells(word.trim_end());
        if width.saturating_sub(offset) >= word_len {
            offset += cells(word);
        } else if word_len > width && !fold {
            if start > 0 {
                breaks.push(start);
            }
            offset = cells(word);
        } else if word_len > width {
            let wchars: Vec<char> = word.chars().collect();
            let chunks: Vec<&[char]> = wchars.chunks(width.max(1)).collect();
            for (i, chunk) in chunks.iter().enumerate() {
                if start > 0 {
                    breaks.push(start);
                }
                if i + 1 == chunks.len() {
                    offset = chunk.len();
                } else {
                    start += chunk.len();
                }
            }
        } else {
            breaks.push(start);
            offset = cells(word);
        }
    }
    let mut lines = vec![];
    let mut prev = 0;
    for b in breaks.into_iter().chain([chars.len()]) {
        if b < prev {
            continue;
        }
        let mut line: String = chars[prev..b].iter().collect();
        // rstrip_end: trailing whitespace past the width goes.
        while cells(&line) > width && line.ends_with(char::is_whitespace) {
            line.pop();
        }
        lines.push(line);
        prev = b;
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

/// `console.print(markup)`.
pub fn print(markup: &str) {
    print_plain(&plain(markup));
}

/// `console.print(Text(...))` — no markup processing.
pub fn print_plain(text: &str) {
    let w = width();
    for line in text.split('\n') {
        for wrapped in wrap(line, w) {
            println!("{wrapped}");
        }
    }
}

fn crop_pad(line: &str, w: usize) -> String {
    let mut out: String = line.chars().take(w).collect();
    let n = cells(&out);
    out.extend(std::iter::repeat_n(' ', w - n));
    out
}

/// Tabs to 4-column stops (Syntax's tab_size).
fn expand_tabs(line: &str) -> String {
    let mut out = String::new();
    let mut col = 0;
    for c in line.chars() {
        if c == '\t' {
            let n = 4 - col % 4;
            out.extend(std::iter::repeat_n(' ', n));
            col += n;
        } else {
            out.push(c);
            col += 1;
        }
    }
    out
}

/// `Syntax(code, line_numbers=..., start_line=...)` rows (no wrapping).
pub fn syntax(code: &str, start_line: Option<i64>) -> Vec<String> {
    let lines: Vec<String> = code.split('\n').map(expand_tabs).collect();
    let Some(start) = start_line else {
        return lines;
    };
    let gutter = (start + code.matches('\n').count() as i64)
        .to_string()
        .len()
        + 2;
    lines
        .iter()
        .enumerate()
        .map(|(i, l)| format!("  {:>w$} {l}", start + i as i64, w = gutter - 2))
        .collect()
}

/// Full-width `Panel` (rounded box, centered title); content is cropped.
pub fn panel(content: &[String], title: Option<&str>) -> Vec<String> {
    let w = width().max(4);
    let inner = w - 4;
    let top = match title {
        Some(t) => {
            let t = format!(" {t} ");
            let excess = inner.saturating_sub(cells(&t));
            let left = excess / 2;
            format!("╭─{}{t}{}─╮", "─".repeat(left), "─".repeat(excess - left))
        }
        None => format!("╭{}╮", "─".repeat(w - 2)),
    };
    let mut out = vec![top];
    for line in content {
        out.push(format!("│ {} │", crop_pad(line, inner)));
    }
    out.push(format!("╰{}╯", "─".repeat(w - 2)));
    out
}

fn print_lines(lines: &[String]) {
    for l in lines {
        println!("{l}");
    }
}

/// Rich `Table()` with a bold header row and the default heavy-head box.
pub struct Table {
    headers: Vec<String>,
    right: Vec<bool>,
    rows: Vec<Vec<String>>,
}

impl Table {
    pub fn new(headers: &[&str]) -> Table {
        Table {
            headers: headers.iter().map(|h| (*h).to_owned()).collect(),
            right: vec![false; headers.len()],
            rows: vec![],
        }
    }

    pub fn right(mut self, column: usize) -> Table {
        self.right[column] = true;
        self
    }

    /// Cells are console markup, as strings given to Rich's `add_row` are.
    pub fn row(&mut self, cells: Vec<String>) {
        self.rows.push(cells.iter().map(|c| plain(c)).collect());
    }

    /// Column widths (padding included), collapsed like Rich's
    /// `_collapse_widths` when the table outgrows the console.
    fn widths(&self) -> Vec<usize> {
        let n = self.headers.len();
        let mut widths: Vec<usize> = (0..n)
            .map(|c| {
                self.rows
                    .iter()
                    .map(|r| cells(&r[c]))
                    .chain([cells(&self.headers[c])])
                    .max()
                    .unwrap_or(0)
                    + 2
            })
            .collect();
        let max_width = width().saturating_sub(n + 1);
        let mut total: usize = widths.iter().sum();
        while total > max_width {
            let excess = total - max_width;
            let max_col = *widths.iter().max().unwrap();
            let second = widths
                .iter()
                .filter(|&&w| w != max_col)
                .max()
                .copied()
                .unwrap_or(0);
            let diff = max_col - second;
            if diff == 0 {
                break;
            }
            let ratios: Vec<usize> = widths.iter().map(|&w| usize::from(w == max_col)).collect();
            widths = ratio_reduce(excess, &ratios, &vec![excess.min(diff); n], &widths);
            total = widths.iter().sum();
        }
        if total > max_width {
            let excess = total - max_width;
            widths = ratio_reduce(excess, &vec![1; n], &widths.clone(), &widths);
        }
        widths
    }

    pub fn render(&self) -> Vec<String> {
        let widths = self.widths();
        let line = |l: &str, m: &str, r: &str, fill: &str| {
            let parts: Vec<String> = widths.iter().map(|&w| fill.repeat(w)).collect();
            format!("{l}{}{r}", parts.join(m))
        };
        let mut out = vec![line("┏", "┳", "┓", "━")];
        out.extend(self.render_row(&self.headers, &widths, "┃"));
        out.push(line("┡", "╇", "┩", "━"));
        for row in &self.rows {
            out.extend(self.render_row(row, &widths, "│"));
        }
        out.push(line("└", "┴", "┘", "─"));
        out
    }

    fn render_row(&self, row: &[String], widths: &[usize], bar: &str) -> Vec<String> {
        let wrapped: Vec<Vec<String>> = row
            .iter()
            .zip(widths)
            .map(|(cell, &w)| cell_lines(cell, w.saturating_sub(2)))
            .collect();
        let height = wrapped.iter().map(Vec::len).max().unwrap_or(1);
        (0..height)
            .map(|i| {
                let parts: Vec<String> = wrapped
                    .iter()
                    .zip(widths)
                    .enumerate()
                    .map(|(c, (lines, &w))| {
                        let text = lines.get(i).map(String::as_str).unwrap_or("");
                        let inner = w.saturating_sub(2);
                        let pad = inner.saturating_sub(cells(text));
                        if self.right[c] {
                            format!(" {}{text} ", " ".repeat(pad))
                        } else {
                            format!(" {text}{} ", " ".repeat(pad))
                        }
                    })
                    .collect();
                format!("{bar}{}{bar}", parts.join(bar))
            })
            .collect()
    }
}

/// A table cell: word-wrapped, an over-long word cut with an ellipsis.
fn cell_lines(text: &str, w: usize) -> Vec<String> {
    if cells(text) <= w {
        return vec![text.to_owned()];
    }
    divide(text, w, false)
        .into_iter()
        .map(|l| {
            if cells(&l) > w {
                let mut cut: String = l.chars().take(w.saturating_sub(1)).collect();
                cut.push('…');
                cut
            } else {
                l
            }
        })
        .collect()
}

fn ratio_reduce(
    total: usize,
    ratios: &[usize],
    maximums: &[usize],
    values: &[usize],
) -> Vec<usize> {
    let ratios: Vec<usize> = ratios
        .iter()
        .zip(maximums)
        .map(|(&r, &m)| if m > 0 { r } else { 0 })
        .collect();
    let mut total_ratio: usize = ratios.iter().sum();
    if total_ratio == 0 {
        return values.to_vec();
    }
    let mut remaining = total as f64;
    let mut out = vec![];
    for ((&ratio, &maximum), &value) in ratios.iter().zip(maximums).zip(values) {
        if ratio > 0 && total_ratio > 0 {
            let share = (ratio as f64 * remaining / total_ratio as f64).round_ties_even();
            let distributed = (maximum as f64).min(share) as usize;
            out.push(value.saturating_sub(distributed));
            remaining -= distributed as f64;
            total_ratio -= ratio;
        } else {
            out.push(value);
        }
    }
    out
}

/// Click/Typer usage error: usage line, hint, boxed message; exit 2.
pub fn usage_error(message: &str) -> ! {
    let w = width().max(10);
    let inner = w - 4;
    let title = " Error ";
    eprintln!("Usage: symbol [OPTIONS] COMMAND [ARGS]...");
    eprintln!("Try 'symbol --help' for help.");
    eprintln!(
        "╭─{title}{}─╮",
        "─".repeat(inner.saturating_sub(cells(title)))
    );
    for line in wrap(message, inner) {
        eprintln!("│ {} │", crop_pad(&line, inner));
    }
    eprintln!("╰{}╯", "─".repeat(w - 2));
    std::process::exit(2);
}

// ---------------------------------------------------------- JSON shapes

/// Which empty values a command's `_to_dict` leaves out.
#[derive(Clone, Copy)]
pub enum Omit {
    /// `v is not None`
    Null,
    /// `v not in (None, "", (), 0)` — also drops False; non-empty tuples stay.
    Falsy,
}

fn omitted(v: &Value, omit: Omit) -> bool {
    match omit {
        Omit::Null => v.is_null(),
        Omit::Falsy => match v {
            Value::Null => true,
            Value::Bool(b) => !b,
            Value::Number(n) => n.as_f64() == Some(0.0),
            Value::String(s) => s.is_empty(),
            Value::Array(a) => a.is_empty(),
            Value::Object(_) => false,
        },
    }
}

/// A dataclass rendered through `asdict` + the command's filter: fields in
/// declaration order, defaults for fields the engine left out.
pub fn shape(source: &Value, fields: &[(&str, Value)], omit: Omit) -> Value {
    let mut out = serde_json::Map::new();
    for (name, default) in fields {
        let v = source
            .get(*name)
            .cloned()
            .unwrap_or_else(|| default.clone());
        if !omitted(&v, omit) {
            out.insert((*name).to_owned(), v);
        }
    }
    Value::Object(out)
}

/// Each object in `list` with exactly `keys`, in that order.
fn reorder(list: &Value, keys: &[&str]) -> Value {
    Value::Array(
        list.as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[])
            .iter()
            .map(|item| {
                Value::Object(
                    keys.iter()
                        .map(|k| {
                            (
                                (*k).to_owned(),
                                item.get(*k).cloned().unwrap_or(Value::Null),
                            )
                        })
                        .collect(),
                )
            })
            .collect(),
    )
}

fn with_nested(mut v: Value, nested: &[(&str, &[&str])]) -> Value {
    for (field, keys) in nested {
        if let Some(list) = v.get(*field) {
            let ordered = reorder(list, keys);
            v[*field] = ordered;
        }
    }
    v
}

pub fn patch_json(result: &Value) -> Value {
    shape(
        result,
        &[
            ("status", json!("")),
            ("file_rel", json!("")),
            ("before_range", json!([0, 0])),
            ("after_range", json!([0, 0])),
            ("new_line_range", json!([0, 0])),
            ("new_content", json!("")),
            ("lines_removed", json!(0)),
            ("lines_added", json!(0)),
            ("diff", json!("")),
            ("error_code", Value::Null),
            ("message", Value::Null),
        ],
        Omit::Null,
    )
}

pub fn delete_json(result: &Value) -> Value {
    let v = with_nested(
        result.clone(),
        &[("callers", &["file", "source_path", "line", "kind"])],
    );
    shape(
        &v,
        &[
            ("status", json!("")),
            ("qualified_path", json!("")),
            ("kind", json!("")),
            ("file_rel", json!("")),
            ("line_range", json!([0, 0])),
            ("callers", json!([])),
            ("diff", json!("")),
            ("lines_removed", json!(0)),
            ("error_code", Value::Null),
            ("message", Value::Null),
            ("candidates", json!([])),
        ],
        Omit::Falsy,
    )
}

pub fn insert_json(result: &Value) -> Value {
    shape(
        result,
        &[
            ("status", json!("")),
            ("anchor_path", json!("")),
            ("anchor_kind", json!("")),
            ("position", json!("")),
            ("file_rel", json!("")),
            ("insert_line", json!(0)),
            ("diff", json!("")),
            ("lines_added", json!(0)),
            ("error_code", Value::Null),
            ("message", Value::Null),
            ("candidates", json!([])),
        ],
        Omit::Falsy,
    )
}

pub fn rename_json(result: &Value) -> Value {
    let v = with_nested(
        result.clone(),
        &[
            ("per_file", &["file", "refs_updated"]),
            (
                "unresolved",
                &["file", "line", "col", "receiver_source", "why"],
            ),
            (
                "skipped_mismatch",
                &[
                    "file",
                    "line",
                    "col",
                    "receiver_source",
                    "resolved_to_qpath",
                ],
            ),
            (
                "affected_interfaces",
                &["interface_qpath", "method_qpath", "file", "line"],
            ),
        ],
    );
    shape(
        &v,
        &[
            ("status", json!("")),
            ("qualified_path", json!("")),
            ("new_qualified_path", json!("")),
            ("files_changed", json!(0)),
            ("refs_updated", json!(0)),
            ("per_file", json!([])),
            ("declaring_file", json!("")),
            ("error_code", Value::Null),
            ("message", Value::Null),
            ("unresolved", json!([])),
            ("skipped_mismatch", json!([])),
            ("affected_interfaces", json!([])),
            ("candidates", json!([])),
        ],
        Omit::Falsy,
    )
}

pub fn replace_json(result: &Value) -> Value {
    let v = with_nested(
        result.clone(),
        &[
            ("per_file", &["file", "refs_updated"]),
            (
                "parameter_renames",
                &[
                    "old",
                    "new",
                    "refs_updated",
                    "skipped_mismatch",
                    "unresolved",
                ],
            ),
        ],
    );
    shape(
        &v,
        &[
            ("status", json!("")),
            ("qualified_path", json!("")),
            ("new_qualified_path", json!("")),
            ("name_changed", json!(false)),
            ("declaring_file", json!("")),
            ("new_signature", json!("")),
            ("kind", json!("")),
            ("files_changed", json!(0)),
            ("refs_updated", json!(0)),
            ("per_file", json!([])),
            ("parameter_renames", json!([])),
            ("signature_changed", json!(false)),
            ("old_parameters", json!([])),
            ("new_parameters", json!([])),
            ("error_code", Value::Null),
            ("message", Value::Null),
            ("candidates", json!([])),
        ],
        Omit::Falsy,
    )
}

pub fn undo_json(result: &Value) -> Value {
    let keys = [
        "status",
        "transaction_id",
        "op",
        "subject",
        "files_restored",
        "files_skipped",
        "error_code",
        "message",
    ];
    Value::Object(
        keys.iter()
            .map(|k| {
                (
                    (*k).to_owned(),
                    result.get(*k).cloned().unwrap_or(Value::Null),
                )
            })
            .collect(),
    )
}

pub fn print_json(v: &Value) {
    println!("{}", dumps_indent(v));
}

// ---------------------------------------------------------- reads

pub fn search_rich(patterns: &[String], hits: &[Value]) {
    if hits.is_empty() {
        print(&format!(
            "[yellow]No matches for[/yellow] {}",
            repr(&patterns.join(" "))
        ));
        return;
    }
    let n = hits.len();
    print(&format!(
        "[dim]{n} match{}[/dim]\n",
        if n != 1 { "es" } else { "" }
    ));
    for (i, h) in hits.iter().enumerate() {
        if i > 0 {
            print(&format!("[dim]{}[/dim]", "─".repeat(78)));
        }
        let sig = match h["signature"].as_str() {
            Some(s) if !s.is_empty() => s,
            _ => h["path"].as_str().unwrap_or(""),
        };
        print(&format!(
            "[dim]{}:{}-{}[/dim]",
            str_of(&h["file"]),
            h["start_line"],
            h["end_line"]
        ));
        print(&format!("[bold]{sig}[/bold]"));
    }
    print("\n[dim]Use[/dim] [bold]symbol code <file:start-end>[/bold] [dim]to fetch a body.[/dim]");
}

fn str_of(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}

pub fn code_not_found(message: &str, json_format: bool) {
    if json_format {
        println!(
            "{}",
            dumps(&json!({"error": "not_found", "message": message}))
        );
    } else {
        print(&format!("[yellow]{message}[/yellow]"));
        print("[dim]Try: symbol search <name>[/dim]");
    }
}

pub fn code_ambiguous(candidates: &[Value], json_format: bool) {
    if json_format {
        print_json(&json!({"error": "ambiguous", "candidates": candidates}));
        return;
    }
    print(&format!(
        "[yellow]{} matches — pass file:start-end or a longer path[/yellow]\n",
        candidates.len()
    ));
    let mut t = Table::new(&["signature", "location"]);
    for c in candidates {
        t.row(vec![
            py(&c["signature"]),
            format!(
                "{}:{}-{}",
                str_of(&c["file"]),
                c["start_line"],
                c["end_line"]
            ),
        ]);
    }
    print_lines(&t.render());
}

pub fn code_rich(hit: &Value, include_refs: bool) {
    let head = match hit["path"].as_str() {
        Some(p) if !p.is_empty() => p.to_owned(),
        _ => py(&hit["kind"]),
    };
    print(&format!(
        "[bold cyan]{head}[/bold cyan]  [dim]{}:{}-{}  ({})[/dim]",
        str_of(&hit["file"]),
        hit["start_line"],
        hit["end_line"],
        py(&hit["kind"])
    ));
    if let Some(note) = hit["note"].as_str().filter(|n| !n.is_empty()) {
        print(&format!("[dim]{note}[/dim]"));
    }
    let start = hit["start_line"].as_i64().unwrap_or(1);
    print_lines(&panel(&syntax(str_of(&hit["body"]), Some(start)), None));
    let imports = hit["imports"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    if !imports.is_empty() {
        print("[bold]imports[/bold]");
        for imp in imports {
            let source = str_of(&imp["source"]);
            let origin = if source.is_empty() {
                String::new()
            } else {
                format!("from {source}")
            };
            print(&format!(
                "  [green]{}[/green] {origin} [dim]:{}[/dim]",
                str_of(&imp["name"]),
                imp["line"]
            ));
        }
    }
    let refs = hit["refs"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    if include_refs && !refs.is_empty() {
        print(&format!(
            "[bold]refs[/bold] [dim]({} external names)[/dim]",
            refs.len()
        ));
        for r in refs.iter().take(30) {
            let tag = if r["kind"] == "name" { "" } else { "." };
            print(&format!(
                "  {tag}{} [dim]:{}[/dim]",
                str_of(&r["name"]),
                r["line"]
            ));
        }
        if refs.len() > 30 {
            print(&format!("  [dim]... {} more[/dim]", refs.len() - 30));
        }
    }
}

fn kind_glyph(kind: &str) -> &'static str {
    match kind {
        "class" => "◆",
        "function" | "async_function" => "ƒ",
        "method" => "•",
        _ => "·",
    }
}

pub fn outline_rich(arg: &str, roots: &[Value]) {
    if roots.is_empty() {
        print(&format!(
            "[yellow]Nothing to outline for[/yellow] {}",
            repr(arg)
        ));
        return;
    }
    fn label(node: &Value) -> String {
        let path = str_of(&node["path"]);
        let sig = match node["signature"].as_str() {
            Some(s) if !s.is_empty() => s,
            _ => path.rsplit('.').next().unwrap_or(path),
        };
        let sig = sig.strip_suffix(':').unwrap_or(sig);
        plain(&format!(
            "{} [cyan]{sig}[/cyan] [dim]{}-{}[/dim]",
            kind_glyph(str_of(&node["kind"])),
            node["start_line"],
            node["end_line"]
        ))
    }
    // A label wraps within the width left after its guides; continuation
    // lines carry the guide that runs past it.
    fn walk(nodes: &[Value], prefix: &str, out: &mut Vec<String>) {
        for (i, node) in nodes.iter().enumerate() {
            let last = i + 1 == nodes.len();
            let (branch, through) = if last {
                ("└── ", "    ")
            } else {
                ("├── ", "│   ")
            };
            let next = format!("{prefix}{through}");
            let room = width().saturating_sub(cells(&next)).max(1);
            for (j, line) in wrap(&label(node), room).iter().enumerate() {
                let guide = if j == 0 { branch } else { through };
                out.push(format!("{prefix}{guide}{line}"));
            }
            let children = node["children"]
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            walk(children, &next, out);
        }
    }
    let mut out = wrap(&plain(&format!("[bold]{arg}[/bold]")), width());
    walk(roots, "", &mut out);
    print_lines(&out);
}

/// commands/loc.py `--format json`. Percentages Python never computed (an
/// uncounted language, or nothing counted) stay the integer 0.
pub fn loc_json(report: &symbol_linguist::loc::LocReport) -> anyhow::Result<Value> {
    let mut data = symbol_linguist::loc::json_value(report, false)?;
    let counted: Vec<_> = report.stats.iter().filter(|s| s.is_counted).collect();
    let no_lines = counted.iter().map(|s| s.sloc).sum::<u64>() == 0;
    let no_bytes = counted.iter().map(|s| s.size).sum::<u64>() == 0;
    for lang in data["languages"].as_array_mut().into_iter().flatten() {
        let counted = lang["is_counted"] == true;
        if !counted || no_lines {
            lang["percentage_lines"] = json!(0);
        }
        if !counted || no_bytes {
            lang["percentage_bytes"] = json!(0);
        }
    }
    Ok(data)
}

pub fn callers_rich(name: &str, hits: &[Value]) {
    if hits.is_empty() {
        print(&format!(
            "[yellow]No references found for[/yellow] {}",
            repr(name)
        ));
        return;
    }
    print(&format!(
        "[dim]{} tier-1 unresolved refs — name-match only, \
         may include unrelated symbols sharing the name[/dim]\n",
        hits.len()
    ));
    let mut t = Table::new(&["caller", "file:line", "kind"]);
    for h in hits.iter().take(200) {
        t.row(vec![
            py(&h["source_path"]),
            format!("{}:{}", str_of(&h["source_file"]), h["ref_line"]),
            py(&h["ref_kind"]),
        ]);
    }
    print_lines(&t.render());
    if hits.len() > 200 {
        print(&format!(
            "[dim]... {} more (use --format json to see all)[/dim]",
            hits.len() - 200
        ));
    }
}

// ---------------------------------------------------------- writes

fn print_error(code: &Value, message: &Value, agent: bool) {
    if agent {
        print!("{}", agent::patch_error(code, message));
        return;
    }
    print(&format!("[red]error[/red]  [bold]{}[/bold]", py(code)));
    if let Some(m) = message.as_str().filter(|m| !m.is_empty()) {
        print(&format!("  {m}"));
    }
}

fn diff_panel(diff: &str) {
    if !diff.is_empty() {
        print_lines(&panel(&syntax(diff, None), Some("diff")));
    }
}

/// patch: a validation error (no request yet) or needs_read_confirmation.
pub fn patch_preflight(pre: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(pre);
        return;
    }
    if pre["status"] == "needs_read_confirmation" {
        let current = str_of(&pre["current_content"]);
        let (a, b) = (&pre["line_range"][0], &pre["line_range"][1]);
        if agent {
            let int = |v: &Value| v.as_i64().unwrap_or(0);
            print!(
                "{}",
                agent::patch_needs_read(
                    str_of(&pre["file"]),
                    (int(a), int(b)),
                    (
                        int(&pre["byte_range"][0]) as usize,
                        int(&pre["byte_range"][1]) as usize
                    ),
                    current,
                )
            );
            return;
        }
        print(&format!(
            "[yellow]needs_read_confirmation[/yellow]  [dim]{}:{a}-{b}[/dim]",
            str_of(&pre["file"])
        ));
        print("[dim]The agent hasn't read this range in this session.[/dim]\n");
        let start = a.as_i64().unwrap_or(1);
        print_lines(&panel(
            &syntax(current, Some(start)),
            Some("current content"),
        ));
        print(
            "\n[dim]After reading, re-run with [bold]--force[/bold] \
             to bypass the cache check.[/dim]",
        );
        return;
    }
    print_error(&pre["error_code"], &pre["message"], agent);
}

pub fn patch_result(result: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(&patch_json(result));
        return;
    }
    if result["status"] == "error" {
        print_error(&result["error_code"], &result["message"], agent);
        return;
    }
    if agent {
        print!("{}", agent::patch_result(result));
        return;
    }
    let diff = str_of(&result["diff"]);
    let applied = result["status"] == "applied";
    print(&format!(
        "[{c}]{t}[/{c}]  [dim]{}[/dim]  [dim]-{} +{} lines[/dim]",
        str_of(&result["file_rel"]),
        result["lines_removed"],
        result["lines_added"],
        c = if applied { "green" } else { "yellow" },
        t = if applied { "applied" } else { "dry run" },
    ));
    diff_panel(diff);
}

fn callers_table(callers: &[Value]) -> Table {
    let mut t = Table::new(&["file:line", "in symbol", "kind"]);
    for c in callers {
        let scope = match c["source_path"].as_str() {
            Some(s) if !s.is_empty() => s.to_owned(),
            _ => "-".to_owned(),
        };
        t.row(vec![
            format!("{}:{}", str_of(&c["file"]), c["line"]),
            scope,
            py(&c["kind"]),
        ]);
    }
    t
}

fn list<'a>(v: &'a Value, key: &str) -> &'a [Value] {
    v[key].as_array().map(Vec::as_slice).unwrap_or(&[])
}

fn line_range(v: &Value) -> (i64, i64) {
    (
        v["line_range"][0].as_i64().unwrap_or(0),
        v["line_range"][1].as_i64().unwrap_or(0),
    )
}

pub fn delete_result(result: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(&delete_json(result));
        return;
    }
    if agent {
        print!("{}", agent::delete_symbol(result));
        return;
    }
    let callers = list(result, "callers");
    let candidates = list(result, "candidates");
    if result["status"] == "error" {
        print_error(&result["error_code"], &result["message"], false);
        if !candidates.is_empty() {
            print("\n[dim]candidates:[/dim]");
            for c in candidates {
                print(&format!("  {}", py(c)));
            }
        }
        if !callers.is_empty() {
            print(&format!("\n[yellow]{} caller(s):[/yellow]", callers.len()));
            print_lines(&callers_table(callers).render());
            print("\n[dim]Use --force to delete anyway (callers will break).[/dim]");
        }
        return;
    }
    let qpath = str_of(&result["qualified_path"]);
    let (a, b) = line_range(result);
    let diff = str_of(&result["diff"]);
    let applied = result["status"] == "applied";
    print(&format!(
        "[{c}]{t}[/{c}]  [bold]{qpath}[/bold]  [dim]({})  {}:{a}-{b}  -{} lines[/dim]",
        str_of(&result["kind"]),
        str_of(&result["file_rel"]),
        result["lines_removed"],
        c = if applied { "green" } else { "yellow" },
        t = if applied { "deleted" } else { "dry run" },
    ));
    if !callers.is_empty() {
        print(&format!(
            "\n[yellow]⚠️  {} reference(s) now broken[/yellow]",
            callers.len()
        ));
        print_lines(&callers_table(callers).render());
        print("\n[dim]Fix with:[/dim] [bold]symbol patch <file>[/bold] [dim]for each[/dim]");
    }
    diff_panel(diff);
}

fn print_candidates_error(result: &Value) {
    print_error(&result["error_code"], &result["message"], false);
    for c in list(result, "candidates") {
        print(&format!("  [dim]candidate:[/dim] {}", py(c)));
    }
}

pub fn insert_result(result: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(&insert_json(result));
        return;
    }
    if agent {
        print!("{}", agent::insert_symbol(result));
        return;
    }
    if result["status"] == "error" {
        print_candidates_error(result);
        return;
    }
    let diff = str_of(&result["diff"]);
    let applied = result["status"] == "applied";
    print(&format!(
        "[{c}]{t}[/{c}]  [dim]{}[/dim]  [bold]{}[/bold]  [dim]({})  {}:{}  +{} lines[/dim]",
        str_of(&result["position"]),
        str_of(&result["anchor_path"]),
        str_of(&result["anchor_kind"]),
        str_of(&result["file_rel"]),
        result["insert_line"],
        result["lines_added"],
        c = if applied { "green" } else { "yellow" },
        t = if applied { "inserted" } else { "dry run" },
    ));
    diff_panel(diff);
}

fn col1(v: &Value) -> i64 {
    v["col"].as_i64().unwrap_or(0) + 1
}

pub fn rename_result(result: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(&rename_json(result));
        return;
    }
    if agent {
        print!("{}", agent::rename_symbol(result));
        return;
    }
    if result["status"] == "error" {
        print_candidates_error(result);
        return;
    }
    let status = str_of(&result["status"]);
    let qpath = str_of(&result["qualified_path"]);
    let new_qpath = str_of(&result["new_qualified_path"]);
    let leaf = qpath.rsplit('.').next().unwrap_or(qpath);
    let per_file = list(result, "per_file");
    let skipped = list(result, "skipped_mismatch");
    let unresolved = list(result, "unresolved");
    let affected = list(result, "affected_interfaces");
    let files_changed = result["files_changed"].as_i64().unwrap_or(0);
    let refs_updated = result["refs_updated"].as_i64().unwrap_or(0);
    let (color, title) = match status {
        "applied" => ("green", "renamed"),
        "dry_run" => ("yellow", "dry run"),
        _ => ("magenta", "needs review"),
    };
    print(&format!(
        "[{color}]{title}[/{color}]  [bold]{qpath}[/bold] → [bold]{new_qpath}[/bold]  \
         [dim]{files_changed} files, {refs_updated} refs[/dim]"
    ));
    if !per_file.is_empty() {
        let mut t = Table::new(&["file", "refs"]).right(1);
        for f in per_file {
            t.row(vec![py(&f["file"]), py(&f["refs_updated"])]);
        }
        print_lines(&t.render());
    }
    if !skipped.is_empty() {
        print(&format!(
            "\n[dim]skipped {} site(s) (receiver bound to a different declaration):[/dim]",
            skipped.len()
        ));
        for s in skipped {
            print(&format!(
                "  [dim]{}:{}:{}[/dim]  [yellow]{}.{leaf}[/yellow] → [bold]{}[/bold]",
                str_of(&s["file"]),
                s["line"],
                col1(s),
                str_of(&s["receiver_source"]),
                str_of(&s["resolved_to_qpath"])
            ));
        }
    }
    if !unresolved.is_empty() {
        print(&format!(
            "\n[bold magenta]needs review[/bold magenta] — {} site(s) the receiver type \
             couldn't be resolved statically:",
            unresolved.len()
        ));
        for u in unresolved {
            print(&format!(
                "  [dim]{}:{}:{}[/dim]  [yellow]{}.{leaf}[/yellow]  [dim]({})[/dim]",
                str_of(&u["file"]),
                u["line"],
                col1(u),
                str_of(&u["receiver_source"]),
                str_of(&u["why"])
            ));
        }
    }
    if !affected.is_empty() {
        print(&format!(
            "\n[bold yellow]affects {} interface contract(s)[/bold yellow] — the target type \
             implements these; renaming leaves them unsatisfied unless you also rename the \
             contract method:",
            affected.len()
        ));
        for a in affected {
            print(&format!(
                "  [yellow]{}[/yellow]  [dim]({}:{})[/dim]",
                str_of(&a["method_qpath"]),
                str_of(&a["file"]),
                a["line"]
            ));
        }
    }
    if status == "applied" {
        print("\n[dim]Undo:[/dim] [bold]symbol undo[/bold]");
    }
}

fn joined_or_none(v: &Value) -> String {
    let items: Vec<String> = v
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[])
        .iter()
        .map(py)
        .collect();
    if items.is_empty() {
        "none".into()
    } else {
        items.join(", ")
    }
}

pub fn replace_result(result: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(&replace_json(result));
        return;
    }
    if agent {
        print!("{}", agent::replace_symbol(result));
        return;
    }
    if result["status"] == "error" {
        print_candidates_error(result);
        return;
    }
    let status = str_of(&result["status"]);
    let qpath = str_of(&result["qualified_path"]);
    let new_qpath = str_of(&result["new_qualified_path"]);
    let name_changed = result["name_changed"] == true;
    let signature = str_of(&result["new_signature"]);
    let per_file = list(result, "per_file");
    let renames = list(result, "parameter_renames");
    let signature_changed = result["signature_changed"] == true;
    let files_changed = result["files_changed"].as_i64().unwrap_or(0);
    let refs_updated = result["refs_updated"].as_i64().unwrap_or(0);
    let kind = str_of(&result["kind"]);
    let applied = status == "applied";
    let arrow = if name_changed {
        format!("[bold]{qpath}[/bold] → [bold]{new_qpath}[/bold]")
    } else {
        format!("[bold]{qpath}[/bold]")
    };
    print(&format!(
        "[{c}]{t}[/{c}]  {arrow}  [dim]({kind})  {files_changed} file(s), {refs_updated} change(s)[/dim]",
        c = if applied { "green" } else { "yellow" },
        t = if applied { "replaced" } else { "dry run" },
    ));
    if !signature.is_empty() {
        print(&format!(
            "  [dim]new signature:[/dim] [cyan]{signature}[/cyan]"
        ));
    }
    if !per_file.is_empty() {
        let mut t = Table::new(&["file", "changes"]).right(1);
        for f in per_file {
            t.row(vec![py(&f["file"]), py(&f["refs_updated"])]);
        }
        print_lines(&t.render());
    }
    for pr in renames {
        let mut line = format!(
            "  [dim]parameter[/dim] [bold]{}[/bold] → [bold]{}[/bold]  [dim]{} kwarg site(s)",
            str_of(&pr["old"]),
            str_of(&pr["new"]),
            pr["refs_updated"]
        );
        if pr["skipped_mismatch"].as_i64().unwrap_or(0) != 0 {
            line.push_str(&format!(", {} skipped", pr["skipped_mismatch"]));
        }
        if pr["unresolved"].as_i64().unwrap_or(0) != 0 {
            line.push_str(&format!(", {} unresolved", pr["unresolved"]));
        }
        line.push_str("[/dim]");
        print(&line);
    }
    if signature_changed {
        print(&format!(
            "  [yellow]signature changed[/yellow] [dim]({} → {}) — kwarg sites NOT updated[/dim]",
            joined_or_none(&result["old_parameters"]),
            joined_or_none(&result["new_parameters"])
        ));
    }
    if applied {
        print("\n[dim]Undo:[/dim] [bold]git reset --hard HEAD~1[/bold]");
    }
}

pub fn undo_result(result: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(&undo_json(result));
        return;
    }
    let restored = list(result, "files_restored");
    let skipped = list(result, "files_skipped");
    if agent {
        println!("status: {}", str_of(&result["status"]));
        for key in ["transaction_id", "op", "subject"] {
            if let Some(v) = result[key].as_str().filter(|v| !v.is_empty()) {
                println!("{key}: {v}");
            }
        }
        if !restored.is_empty() {
            println!("restored: {}", restored.len());
            for f in restored {
                println!("  - {}", py(f));
            }
        }
        if !skipped.is_empty() {
            println!("skipped: {}", skipped.len());
            for f in skipped {
                println!("  - {}", py(f));
            }
        }
        for key in ["error_code", "message"] {
            if let Some(v) = result[key].as_str().filter(|v| !v.is_empty()) {
                println!("{key}: {v}");
            }
        }
        return;
    }
    match str_of(&result["status"]) {
        "nothing_to_undo" => print("[dim]nothing to undo[/dim]"),
        "error" => {
            print(&format!(
                "[red]undo failed[/red]  [bold]{}[/bold]",
                py(&result["error_code"])
            ));
            if let Some(m) = result["message"].as_str().filter(|m| !m.is_empty()) {
                print(&format!("  {m}"));
            }
        }
        _ => {
            print(&format!(
                "[green]undone[/green]  [dim]{}[/dim]  {}",
                py(&result["op"]),
                result["subject"].as_str().unwrap_or("")
            ));
            print(&format!("[dim]restored {} file(s)[/dim]", restored.len()));
            for f in restored {
                print(&format!("  [dim]•[/dim] {}", py(f)));
            }
            if !skipped.is_empty() {
                print(&format!(
                    "[yellow]skipped {} file(s) (I/O error)[/yellow]",
                    skipped.len()
                ));
            }
        }
    }
}

/// refresh: `{status, source, symbols, files, transactions_cleared}`.
pub fn refresh_result(result: &Value, json_format: bool, agent: bool) {
    if json_format {
        print_json(result);
        return;
    }
    if agent {
        for key in [
            "status",
            "source",
            "symbols",
            "files",
            "transactions_cleared",
        ] {
            println!("{key}: {}", py(&result[key]));
        }
        return;
    }
    print(&format!(
        "[green]refreshed[/green]  [dim]{}[/dim]  {} symbols across {} files",
        str_of(&result["source"]),
        result["symbols"],
        result["files"]
    ));
    let cleared = result["transactions_cleared"].as_i64().unwrap_or(0);
    if cleared != 0 {
        print(&format!("[dim]cleared {cleared} transaction(s)[/dim]"));
    }
}

// ---------------------------------------------------------- loc

fn thousands(n: u64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// `f"{x:.0f}"` — round half to even, like Python.
fn fixed0(x: f64) -> String {
    format!("{}", x.round_ties_even())
}

/// commands/loc.py's rich view: name panel, language bar, totals, a
/// borderless table cropped to the console, then the biggest files.
pub fn loc_rich(report: &symbol_linguist::loc::LocReport, project_name: &str) {
    let w = width();
    println!();
    let title = format!("  symbol loc — {project_name}/");
    let bar = "─".repeat(cells(&title) + 2);
    println!("╭{bar}╮\n│ {title} │\n╰{bar}╯");
    let stats: Vec<_> = report.stats.iter().collect();
    if stats.is_empty() {
        println!();
        println!("  (no recognized source files)");
        println!();
        return;
    }
    let mut sorted = stats.clone();
    sorted.sort_by_key(|s| std::cmp::Reverse(s.sloc));
    let total_sloc: u64 = stats.iter().map(|s| s.sloc).sum();
    let total_loc: u64 = stats.iter().map(|s| s.loc).sum();
    let total_files: u64 = stats.iter().map(|s| s.files).sum();
    let total_size: u64 = stats.iter().map(|s| s.size).sum();
    let code_langs = stats
        .iter()
        .filter(|s| s.lang_type == "programming")
        .count();
    let avg = total_loc.checked_div(total_files).unwrap_or(0);

    println!();
    let mut language_bar = String::new();
    if total_sloc == 0 {
        language_bar = "░".repeat(60);
    } else {
        let mut remaining: i64 = 60;
        for (i, s) in sorted.iter().enumerate() {
            if s.sloc == 0 {
                continue;
            }
            let width = if i + 1 == sorted.len() {
                remaining
            } else {
                let share = (s.sloc as f64 / total_sloc as f64 * 60.0).round_ties_even() as i64;
                share.max(1).min(remaining)
            };
            remaining -= width;
            language_bar.push_str(&"█".repeat(width.max(0) as usize));
            if remaining <= 0 {
                break;
            }
        }
    }
    print_plain(&format!("  {language_bar}"));
    println!();
    print_plain(&format!(
        "  {} sloc  {} lines  {} blank  {} files  {avg} avg lines/file  {code_langs}/{} langs",
        thousands(total_sloc),
        thousands(total_loc),
        thousands(total_loc - total_sloc),
        thousands(total_files),
        sorted.len()
    ));
    println!();

    let mut rows: Vec<[String; 7]> = vec![[
        "Language".into(),
        "Type".into(),
        "Files".into(),
        "SLOC".into(),
        "Lines".into(),
        "Size".into(),
        "%".into(),
    ]];
    for s in &sorted {
        let pct = if s.is_counted && s.percentage_lines != 0.0 {
            format!("{:.1}%", s.percentage_lines)
        } else {
            String::new()
        };
        rows.push([
            format!("● {}", s.name),
            s.lang_type.clone(),
            s.files.to_string(),
            s.sloc.to_string(),
            s.loc.to_string(),
            format!("{}K", fixed0(s.size as f64 / 1024.0)),
            pct,
        ]);
    }
    rows.push([
        "  Total".into(),
        String::new(),
        total_files.to_string(),
        thousands(total_sloc),
        thousands(total_loc),
        format!("{}K", fixed0(total_size as f64 / 1024.0)),
        String::new(),
    ]);
    let min_widths = [22, 12, 0, 0, 0, 0, 0];
    let col_widths: Vec<usize> = (0..7)
        .map(|c| {
            rows.iter()
                .map(|r| cells(&r[c]))
                .max()
                .unwrap_or(0)
                .max(min_widths[c])
        })
        .collect();
    for row in &rows {
        let mut line = String::new();
        for (c, cell) in row.iter().enumerate() {
            let pad = col_widths[c] - cells(cell);
            if c >= 2 {
                line.push_str(&format!("  {}{cell}  ", " ".repeat(pad)));
            } else {
                line.push_str(&format!("  {cell}{}  ", " ".repeat(pad)));
            }
        }
        println!("{}", line.chars().take(w).collect::<String>());
    }

    let mut code_files: Vec<(&str, u64, &str)> = vec![];
    for s in &stats {
        if s.lang_type == "programming" {
            for f in &s.file_stats {
                code_files.push((&f.path, f.lines, &s.name));
            }
        }
    }
    if !code_files.is_empty() {
        code_files.sort_by_key(|f| std::cmp::Reverse(f.1));
        println!();
        println!("  📄 BIGGEST FILES");
        println!();
        let root = report.root.to_string_lossy().into_owned();
        for (path, lines, lang) in code_files.iter().take(5) {
            let rel = path.strip_prefix(root.as_str()).unwrap_or(path);
            let rel = rel.trim_start_matches('/');
            print_plain(&format!(
                "    {rel:<50} {} lines  ● {lang}",
                thousands(*lines)
            ));
        }
    }
    println!();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markup_tags_drop_and_escapes_survive() {
        assert_eq!(plain("[bold]a[/bold] [dim]b[/]"), "a b");
        assert_eq!(plain("list[int] and [0] \\[x]"), "list and [0] [x]");
    }

    #[test]
    fn wrap_keeps_trailing_space_like_rich() {
        let text = format!("  {}", "word ".repeat(30));
        let lines = wrap(&text, 80);
        assert_eq!(lines[0].len(), 77);
        assert!(lines[0].ends_with("word "));
        assert_eq!(wrap(&"x".repeat(100), 80)[1], "x".repeat(20));
    }

    #[test]
    fn falsy_omission_keeps_non_empty_tuples() {
        let v = json!({"status": "error", "line_range": [0, 0], "callers": [], "lines_removed": 0});
        let shaped = delete_json(&v);
        assert_eq!(shaped, json!({"status": "error", "line_range": [0, 0]}));
    }

    #[test]
    fn table_matches_rich_layout() {
        let mut t = Table::new(&["file", "refs"]).right(1);
        t.row(vec!["a.py".into(), "12".into()]);
        assert_eq!(
            t.render(),
            vec![
                "┏━━━━━━┳━━━━━━┓",
                "┃ file ┃ refs ┃",
                "┡━━━━━━╇━━━━━━┩",
                "│ a.py │   12 │",
                "└──────┴──────┘",
            ]
        );
    }

    #[test]
    fn syntax_gutter_width_follows_last_line() {
        assert_eq!(syntax("a\nb", Some(7)), vec!["  7 a", "  8 b"]);
        assert_eq!(syntax("a\n\tb\nc", Some(98))[1], "   99     b");
    }
}
