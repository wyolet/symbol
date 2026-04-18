"""Normalize agent-supplied write content.

Agents send code with inconsistent leading/trailing whitespace and arbitrary
first-line indentation. The write tools need a single canonical form: flush-left,
re-indented to the target scope, with no leading/trailing blank lines and a
single trailing newline.

This module owns *content* normalization. Blank-line spacing *between* the
content and its surrounding code is handled separately by `_blank_lines`.
"""


def normalize_content(content: str | bytes, target_indent: str) -> bytes:
    """Return `content` flush-left-then-reindented to `target_indent`.

    - Strips leading and trailing blank lines (whitespace-only).
    - Dedents by the minimum indent across non-blank lines, so a first line
      with extra leading whitespace doesn't poison the rest.
    - Re-indents every non-blank line to `target_indent`.
    - Blank interior lines stay blank (no indent injected).
    - Always ends with a single `\\n` if non-empty.
    """
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = content

    lines = text.splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return b""

    non_blank = [ln for ln in lines if ln.strip()]
    min_indent = min(_leading_ws_len(ln) for ln in non_blank)

    out: list[str] = []
    for ln in lines:
        if not ln.strip():
            out.append("\n")
            continue
        out.append(target_indent + ln[min_indent:])

    result = "".join(out)
    if not result.endswith("\n"):
        result += "\n"
    return result.encode("utf-8")


def _leading_ws_len(line: str) -> int:
    i = 0
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1
    return i
