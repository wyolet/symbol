"""MCP server — expose symbol as agent-facing tools.

Transport: stdio. Lifetime: long-lived process, one project root per
server. Tools wrap the reads/ and writes/ engines directly (no command
rendering layer) and return structured JSON with machine-parseable
error codes.
"""
