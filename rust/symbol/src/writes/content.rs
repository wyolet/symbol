//! Normalize agent-supplied content and probe target indentation for replace-symbol.

/// normalize_content: strip leading/trailing blank lines, dedent by the
/// minimum indent of non-blank lines, re-indent to `target_indent`, single
/// trailing newline. Interior blank lines stay bare "\n".
pub fn normalize_content(content: &str, target_indent: &str) -> Vec<u8> {
    let mut lines: Vec<&str> = content.split_inclusive('\n').collect();
    // Handle a final line without terminator: split_inclusive keeps it.
    while lines.first().is_some_and(|l| l.trim().is_empty()) {
        lines.remove(0);
    }
    while lines.last().is_some_and(|l| l.trim().is_empty()) {
        lines.pop();
    }
    if lines.is_empty() {
        return vec![];
    }
    let min_indent = lines
        .iter()
        .filter(|l| !l.trim().is_empty())
        .map(|l| leading_ws_len(l))
        .min()
        .unwrap_or(0);

    let mut out = String::new();
    for line in &lines {
        if line.trim().is_empty() {
            out.push('\n');
            continue;
        }
        out.push_str(target_indent);
        out.push_str(&line[min_indent.min(line.len())..]);
    }
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.into_bytes()
}

fn leading_ws_len(line: &str) -> usize {
    line.bytes()
        .take_while(|&b| b == b' ' || b == b'\t')
        .count()
}

/// _indent_at_byte: leading whitespace of the line containing `byte_pos`.
pub fn indent_at_byte(data: &[u8], byte_pos: usize) -> String {
    let line_start = data[..byte_pos.min(data.len())]
        .iter()
        .rposition(|&b| b == b'\n')
        .map(|i| i + 1)
        .unwrap_or(0);
    let mut out = String::new();
    for &b in &data[line_start..] {
        if b == b' ' || b == b'\t' {
            out.push(b as char);
        } else {
            break;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_like_python() {
        assert_eq!(
            normalize_content("\n\n    def f():\n        pass\n\n", "  "),
            b"  def f():\n      pass\n".to_vec()
        );
        assert_eq!(normalize_content("x = 1", ""), b"x = 1\n".to_vec());
        assert_eq!(normalize_content("\n  \n", ""), Vec::<u8>::new());
        // interior blank lines stay bare
        assert_eq!(
            normalize_content("a\n\nb", "    "),
            b"    a\n\n    b\n".to_vec()
        );
    }

    #[test]
    fn indent_probe() {
        let src = b"class A:\n    def f(self):\n        pass\n";
        assert_eq!(indent_at_byte(src, 13), "    "); // inside "    def f"
        assert_eq!(indent_at_byte(src, 0), "");
    }
}
