//! Port of strategy/shebang.py, including its quirks: only ONE trailing
//! `.digits` group is stripped (`python3.11` → `python3`), and a spaced
//! `#! /usr/bin/env python` yields `/usr/bin/env` from the env-args scan
//! (bug-faithful — Python splits the whole line and takes token [1]).

use std::sync::LazyLock;

use super::{narrow, Strategy};
use crate::blob::Blob;
use crate::language::{LangId, Registry};
use crate::lines::py_splitlines;

static SHEBANG: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"^#!\s*(\S+)").unwrap());
static VERSION_SUFFIX: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"\.\d+$").unwrap());
static SH_EXEC_REWRAP: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(r#"exec (\w+)[\s"']+\$0[\s"']+\$@"#).unwrap()
});
static EXEC_ANY: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"exec (\w+)").unwrap());

pub struct ShebangStrategy;

impl Strategy for ShebangStrategy {
    fn call(&self, blob: &Blob, candidates: &[LangId], reg: &Registry) -> Vec<LangId> {
        if blob.is_symlink {
            return vec![];
        }
        match get_interpreter(blob.data()) {
            Some(interp) => narrow(reg.find_by_interpreter(&interp), candidates),
            None => vec![],
        }
    }
}

pub fn get_interpreter(data: &str) -> Option<String> {
    if !data.starts_with("#!") {
        return None;
    }
    let lines = py_splitlines(data);
    let (a, b) = *lines.first()?;
    let shebang_line = &data[a..b];

    let path = SHEBANG.captures(shebang_line)?.get(1)?.as_str();
    let mut interpreter = path.split('/').next_back().unwrap_or(path).to_owned();

    if interpreter == "env" {
        let args: Vec<&str> = shebang_line.split_whitespace().skip(1).collect();
        interpreter = args
            .iter()
            .find(|arg| !arg.starts_with('-') && !arg.contains('='))?
            .to_string();
    }
    if interpreter.is_empty() {
        return None;
    }

    interpreter = VERSION_SUFFIX.replace(&interpreter, "").into_owned();

    if interpreter == "sh" {
        let first5 = lines
            .iter()
            .take(5)
            .any(|&(a, b)| SH_EXEC_REWRAP.is_match(&data[a..b]));
        if first5 {
            interpreter = EXEC_ANY.captures(data)?.get(1)?.as_str().to_owned();
        }
    }

    if interpreter == "osascript" && shebang_line.contains("-l") {
        return None;
    }

    Some(interpreter)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interpreter_extraction() {
        assert_eq!(get_interpreter("#!/usr/bin/python3\n"), Some("python3".into()));
        assert_eq!(
            get_interpreter("#!/usr/bin/env python3.11\n"),
            Some("python3".into())
        );
        assert_eq!(
            get_interpreter("#!/usr/bin/env -S deno run\n"),
            Some("deno".into())
        );
        assert_eq!(get_interpreter("#!/bin/bash\necho"), Some("bash".into()));
        assert_eq!(get_interpreter("no shebang"), None);
        assert_eq!(get_interpreter("#!/usr/bin/env\n"), None);
        // osascript with -l is rejected
        assert_eq!(get_interpreter("#!/usr/bin/osascript -l JavaScript\n"), None);
        // sh exec-rewrap resolves the real interpreter
        assert_eq!(
            get_interpreter("#!/bin/sh\n# next line re-execs\nexec perl \"$0\" \"$@\"\n"),
            Some("perl".into())
        );
        // bug-faithful: spaced shebang + env picks the path token
        assert_eq!(
            get_interpreter("#! /usr/bin/env python\n"),
            Some("/usr/bin/env".into())
        );
    }
}
