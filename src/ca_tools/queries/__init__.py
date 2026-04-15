"""Query layer — AST-native lookups over a codebase.

Sibling to checkers/. Builds a compact symbol index once, then answers
find / outline / callers queries without re-parsing. Never stores body
text; always slices from source files by byte range.
"""
