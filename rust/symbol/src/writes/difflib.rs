//! Port of CPython's `difflib.SequenceMatcher` (no junk function, autojunk
//! on) and `difflib.unified_diff` over lines. Diffs are user-visible in
//! every write result, and other diff algorithms pick different — equally
//! valid — alignments around repeated lines such as blanks.

use std::collections::HashMap;

type Opcode = (&'static str, usize, usize, usize, usize);

struct SequenceMatcher<'a> {
    a: &'a [&'a str],
    b: &'a [&'a str],
    b2j: HashMap<&'a str, Vec<usize>>,
}

impl<'a> SequenceMatcher<'a> {
    fn new(a: &'a [&'a str], b: &'a [&'a str]) -> Self {
        let mut b2j: HashMap<&str, Vec<usize>> = HashMap::new();
        for (i, line) in b.iter().enumerate() {
            b2j.entry(line).or_default().push(i);
        }
        // autojunk: in sequences of 200+ items, items making up more than
        // 1% of b are dropped from the index (but not treated as junk).
        if b.len() >= 200 {
            let ntest = b.len() / 100 + 1;
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
                let k = j.checked_sub(1).and_then(|p| j2len.get(&p)).unwrap_or(&0) + 1;
                newj2len.insert(j, k);
                if k > bestsize {
                    besti = i + 1 - k;
                    bestj = j + 1 - k;
                    bestsize = k;
                }
            }
            j2len = newj2len;
        }
        // Extend over equal neighbours (popular lines are not in b2j but are
        // not junk either, so they extend the match here).
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
        let mut collapsed = vec![];
        for (i2, j2, k2) in blocks {
            if i1 + k1 == i2 && j1 + k1 == j2 {
                k1 += k2;
            } else {
                if k1 > 0 {
                    collapsed.push((i1, j1, k1));
                }
                (i1, j1, k1) = (i2, j2, k2);
            }
        }
        if k1 > 0 {
            collapsed.push((i1, j1, k1));
        }
        collapsed.push((la, lb, 0));
        collapsed
    }

    fn opcodes(&self) -> Vec<Opcode> {
        let (mut i, mut j) = (0, 0);
        let mut out = vec![];
        for (ai, bj, size) in self.matching_blocks() {
            let tag = if i < ai && j < bj {
                "replace"
            } else if i < ai {
                "delete"
            } else if j < bj {
                "insert"
            } else {
                ""
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
            codes.push(("equal", 0, 1, 0, 1));
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
        let mut groups = vec![];
        let mut group = vec![];
        for (tag, mut i1, i2, mut j1, j2) in codes {
            if tag == "equal" && i2 - i1 > 2 * n {
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
    let length = stop - start;
    match length {
        1 => format!("{}", start + 1),
        0 => format!("{start},0"),
        _ => format!("{},{length}", start + 1),
    }
}

/// `"".join(difflib.unified_diff(a, b, fromfile, tofile, n=n))` where `a`
/// and `b` are lines with their endings kept.
pub fn unified_diff(a: &[&str], b: &[&str], fromfile: &str, tofile: &str, n: usize) -> String {
    let matcher = SequenceMatcher::new(a, b);
    let mut out = String::new();
    for (index, group) in matcher.grouped_opcodes(n).iter().enumerate() {
        if index == 0 {
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

    fn lines(s: &str) -> Vec<&str> {
        s.split_inclusive('\n').collect()
    }

    #[test]
    fn matches_python_unified_diff() {
        // Expected text produced by CPython difflib for the same inputs.
        let a = lines("a\nb\nc\nd\ne\nf\ng\nh\n");
        let b = lines("a\nB\nc\nd\ne\nf\ng\nh\ni\n");
        assert_eq!(
            unified_diff(&a, &b, "x", "y", 1),
            "--- x\n+++ y\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n@@ -8 +8,2 @@\n h\n+i\n"
        );
        assert_eq!(unified_diff(&a, &a, "x", "y", 3), "");
        let empty: Vec<&str> = vec![];
        assert_eq!(
            unified_diff(&empty, &lines("x\n"), "x", "y", 3),
            "--- x\n+++ y\n@@ -0,0 +1 @@\n+x\n"
        );
    }

    #[test]
    fn prefers_the_first_longest_block_like_python() {
        // T removed between blank lines: difflib keeps the earliest blank
        // run aligned, which is what Python prints.
        let a = lines("x\n\n\nT\n\n\ny\n");
        let b = lines("x\n\n\n\ny\n");
        assert_eq!(
            unified_diff(&a, &b, "x", "y", 5),
            "--- x\n+++ y\n@@ -1,7 +1,5 @@\n x\n \n \n-T\n-\n \n y\n"
        );
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
