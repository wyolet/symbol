//! CPython's `difflib.unified_diff` over `str.splitlines(keepends=True)`,
//! so diffs align exactly as the Python implementation's do (Myers-based
//! diffs place moved blank lines differently).

use std::collections::HashMap;

/// `str.splitlines(keepends=True)`: every Unicode line boundary Python
/// recognizes, `\r\n` as one.
pub fn splitlines_keepends(text: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let mut iter = text.char_indices().peekable();
    while let Some((i, c)) = iter.next() {
        let end = match c {
            '\r' => match iter.peek() {
                Some(&(j, '\n')) => {
                    iter.next();
                    j + 1
                }
                _ => i + 1,
            },
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}'
            | '\u{2029}' => i + c.len_utf8(),
            _ => continue,
        };
        out.push(&text[start..end]);
        start = end;
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

type Opcode = (&'static str, usize, usize, usize, usize);

struct SequenceMatcher<'a> {
    a: &'a [&'a str],
    b: &'a [&'a str],
    b2j: HashMap<&'a str, Vec<usize>>,
}

impl<'a> SequenceMatcher<'a> {
    fn new(a: &'a [&'a str], b: &'a [&'a str]) -> Self {
        let mut b2j: HashMap<&str, Vec<usize>> = HashMap::new();
        for (i, elt) in b.iter().enumerate() {
            b2j.entry(elt).or_default().push(i);
        }
        // autojunk: in long sequences, lines more frequent than 1% + 1 are
        // "popular" and never seed a match.
        let n = b.len();
        if n >= 200 {
            let ntest = n / 100 + 1;
            b2j.retain(|_, idxs| idxs.len() <= ntest);
        }
        SequenceMatcher { a, b, b2j }
    }

    fn find_longest_match(
        &self,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let (a, b) = (self.a, self.b);
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, line) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            for &j in self.b2j.get(line).map(Vec::as_slice).unwrap_or(&[]) {
                if j < blo {
                    continue;
                }
                if j >= bhi {
                    break;
                }
                let k = j
                    .checked_sub(1)
                    .and_then(|p| j2len.get(&p))
                    .copied()
                    .unwrap_or(0)
                    + 1;
                newj2len.insert(j, k);
                if k > bestsize {
                    besti = i + 1 - k;
                    bestj = j + 1 - k;
                    bestsize = k;
                }
            }
            j2len = newj2len;
        }
        // No junk is configured, so popular lines extend a match like any other.
        while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
            besti -= 1;
            bestj -= 1;
            bestsize += 1;
        }
        while besti + bestsize < ahi
            && bestj + bestsize < bhi
            && a[besti + bestsize] == b[bestj + bestsize]
        {
            bestsize += 1;
        }
        (besti, bestj, bestsize)
    }

    fn matching_blocks(&self) -> Vec<(usize, usize, usize)> {
        let (la, lb) = (self.a.len(), self.b.len());
        let mut queue = vec![(0, la, 0, lb)];
        let mut blocks = vec![];
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let (i, j, k) = self.find_longest_match(alo, ahi, blo, bhi);
            if k > 0 {
                blocks.push((i, j, k));
                if alo < i && blo < j {
                    queue.push((alo, i, blo, j));
                }
                if i + k < ahi && j + k < bhi {
                    queue.push((i + k, ahi, j + k, bhi));
                }
            }
        }
        blocks.sort_unstable();
        let (mut i1, mut j1, mut k1) = (0, 0, 0);
        let mut out = vec![];
        for (i2, j2, k2) in blocks {
            if i1 + k1 == i2 && j1 + k1 == j2 {
                k1 += k2;
            } else {
                if k1 > 0 {
                    out.push((i1, j1, k1));
                }
                (i1, j1, k1) = (i2, j2, k2);
            }
        }
        if k1 > 0 {
            out.push((i1, j1, k1));
        }
        out.push((la, lb, 0));
        out
    }

    fn opcodes(&self) -> Vec<Opcode> {
        let (mut i, mut j) = (0, 0);
        let mut out = vec![];
        for (ai, bj, size) in self.matching_blocks() {
            let tag = match (i < ai, j < bj) {
                (true, true) => "replace",
                (true, false) => "delete",
                (false, true) => "insert",
                _ => "",
            };
            if !tag.is_empty() {
                out.push((tag, i, ai, j, bj));
            }
            (i, j) = (ai + size, bj + size);
            if size > 0 {
                out.push(("equal", ai, i, bj, j));
            }
        }
        out
    }

    fn grouped_opcodes(&self, n: usize) -> Vec<Vec<Opcode>> {
        let mut codes = self.opcodes();
        if codes.is_empty() {
            codes = vec![("equal", 0, 1, 0, 1)];
        }
        if let Some(first) = codes.first_mut() {
            if first.0 == "equal" {
                let (tag, i1, i2, j1, j2) = *first;
                *first = (
                    tag,
                    i1.max(i2.saturating_sub(n)),
                    i2,
                    j1.max(j2.saturating_sub(n)),
                    j2,
                );
            }
        }
        if let Some(last) = codes.last_mut() {
            if last.0 == "equal" {
                let (tag, i1, i2, j1, j2) = *last;
                *last = (tag, i1, i2.min(i1 + n), j1, j2.min(j1 + n));
            }
        }
        let nn = n + n;
        let mut groups = vec![];
        let mut group = vec![];
        for (tag, mut i1, i2, mut j1, j2) in codes {
            if tag == "equal" && i2 - i1 > nn {
                group.push((tag, i1, i2.min(i1 + n), j1, j2.min(j1 + n)));
                groups.push(std::mem::take(&mut group));
                i1 = i1.max(i2.saturating_sub(n));
                j1 = j1.max(j2.saturating_sub(n));
            }
            group.push((tag, i1, i2, j1, j2));
        }
        let only_equal = group.len() == 1 && group[0].0 == "equal";
        if !group.is_empty() && !only_equal {
            groups.push(group);
        }
        groups
    }
}

fn format_range(start: usize, stop: usize) -> String {
    let mut beginning = start + 1;
    let length = stop - start;
    if length == 1 {
        return beginning.to_string();
    }
    if length == 0 {
        beginning -= 1;
    }
    format!("{beginning},{length}")
}

/// `"".join(difflib.unified_diff(a, b, fromfile, tofile, n=context))`.
pub fn unified_diff(a: &[&str], b: &[&str], fromfile: &str, tofile: &str, n: usize) -> String {
    let matcher = SequenceMatcher::new(a, b);
    let mut out = String::new();
    for (g, group) in matcher.grouped_opcodes(n).iter().enumerate() {
        if g == 0 {
            out.push_str(&format!("--- {fromfile}\n+++ {tofile}\n"));
        }
        let (first, last) = (group[0], group[group.len() - 1]);
        out.push_str(&format!(
            "@@ -{} +{} @@\n",
            format_range(first.1, last.2),
            format_range(first.3, last.4)
        ));
        for &(tag, i1, i2, j1, j2) in group {
            if tag == "equal" {
                for line in &a[i1..i2] {
                    out.push(' ');
                    out.push_str(line);
                }
                continue;
            }
            if tag == "replace" || tag == "delete" {
                for line in &a[i1..i2] {
                    out.push('-');
                    out.push_str(line);
                }
            }
            if tag == "replace" || tag == "insert" {
                for line in &b[j1..j2] {
                    out.push('+');
                    out.push_str(line);
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_matches_python() {
        assert_eq!(
            splitlines_keepends("a\r\nb\rc\x0cd\u{2028}e"),
            vec!["a\r\n", "b\r", "c\x0c", "d\u{2028}", "e"]
        );
        assert!(splitlines_keepends("").is_empty());
    }

    #[test]
    fn deleted_block_aligns_like_difflib() {
        // python: difflib.unified_diff(old, new, n=1) on these lines
        let old = [
            "x\n",
            "\n",
            "\n",
            "def f():\n",
            "    pass\n",
            "\n",
            "\n",
            "y\n",
        ];
        let new = ["x\n", "\n", "\n", "\n", "\n", "\n", "y\n"];
        let diff = unified_diff(&old, &new, "a", "b", 1);
        assert_eq!(
            diff,
            "--- a\n+++ b\n@@ -3,4 +3,3 @@\n \n-def f():\n-    pass\n+\n \n"
        );
    }

    #[test]
    fn missing_final_newline_runs_on_like_python() {
        let diff = unified_diff(&["a"], &["b"], "x", "y", 3);
        assert_eq!(diff, "--- x\n+++ y\n@@ -1 +1 @@\n-a+b");
    }
}
