# Feature harness cases

Run all checked-in Rust feature scenarios:

```bash
make test-features
```

This writes the complete command, timing, JSON output, assertion, and retained
failure-project details to `.tmp/feature-report.json`. CI uploads the same file
as an artifact on both passing and failing runs.

Run one scenario and write a detailed JSON report:

```bash
cargo run --manifest-path rust/Cargo.toml -p symbol-harness -- \
  scripts/feature_cases/python_core.json \
  --scenario rename_and_undo \
  --report .tmp/feature-report.json
```

Each scenario receives a fresh copy of its configured fixture and is indexed
automatically unless `"auto_index": false` is set. Steps run sequentially, so
they can exercise state transitions such as rename -> undo -> code.

## Step shape

```json
{
  "id": "apply_after_read",
  "op": "patch",
  "read": "app.service.persist",
  "args": {
    "file": "app/service.py",
    "range": "12-12",
    "content": "    return \"patched\""
  },
  "expect": {
    "exit_code": 0,
    "json": {
      "status": "applied",
      "file_rel": "app/service.py"
    },
    "files": {
      "app/service.py": {
        "contains": ["return \"patched\""],
        "not_contains": ["return user.save"]
      }
    },
    "tree_changed": true
  }
}
```

`expect.json` uses dotted selectors. Numeric components index arrays and
`length` returns the size of a list, object, or string. `tree_equals` compares
the source tree with a named snapshot; every auto-indexed scenario receives an
`initial` snapshot. Use `capture_tree` on a step to create more snapshots.

Harness-only `append-file` and `write-file` operations model external edits
for refresh scenarios. The accepted product operations are explicitly listed
in `rust/harness/src/main.rs`; unknown operations are rejected during
configuration validation. `multi-patch` accepts one file plus a JSON edit
array and applies all non-overlapping edits atomically.

`real_world_pilot.json` is a commit-backed external-corpus dataset. Fetch the
corpus mirrors first, then run it with:

```bash
make test-real-world
```
