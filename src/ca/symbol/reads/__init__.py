"""Read surface — AST-native lookups over the symbol index.

Sibling to writes/. Thin orchestration: query SymbolIndex for row ids,
return shaped dicts. Language-specific work (docstring stripping, etc.)
goes through the adapter; row shaping (tree building, payloads) goes
through SymbolIndex methods.
"""
