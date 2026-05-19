# OpenCode integration

`symbol mcp` is a local stdio MCP server, so OpenCode can launch it from the
`mcp` section in `opencode.json`. OpenCode documents local MCP servers as
`type: "local"` entries with a `command` array in its
[MCP server configuration](https://opencode.ai/docs/mcp-servers/).

## Prerequisites

Install the `symbol` CLI first:

```bash
uv tool install wyolet-symbol
```

Or, when testing from a local checkout of this repository, run the examples with
`uv run symbol` instead of `symbol`.

## Project config

Add a project-level `opencode.json` next to the codebase you want `symbol` to
index:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "symbol": {
      "type": "local",
      "command": ["symbol", "mcp", "--root", "."],
      "enabled": true
    }
  }
}
```

OpenCode runs the local MCP command from the project directory, so `--root "."`
serves the current codebase. Use an absolute path if you keep the config in your
global OpenCode config:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "symbol": {
      "type": "local",
      "command": ["symbol", "mcp", "--root", "/path/to/project"],
      "enabled": true
    }
  }
}
```

For a source checkout of `symbol` itself, this variant is convenient:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "symbol": {
      "type": "local",
      "command": ["uv", "run", "symbol", "mcp", "--root", "."],
      "enabled": true
    }
  }
}
```

## Verify

Run:

```bash
opencode mcp list
```

You should see `symbol` connected. OpenCode registers MCP tools with the server
name as a prefix, so `symbol` tools are exposed as `symbol_*`.

## Notes

- The MCP tools work through OpenCode's regular MCP support; no OAuth or login
  is needed for this local server.
- The Claude Code plugin's skill and hook nudges are not part of this OpenCode
  setup. OpenCode receives the MCP tool descriptions, but it does not install
  Claude-specific skills or hooks.
- `symbol` is static analysis only; serving a project through MCP does not
  import or execute the target code.
