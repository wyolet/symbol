# symbol-dataset

Rust pipeline for producing commit-backed, real-world task cases.

```bash
# 1. Mirror external corpora locally.
cargo run --manifest-path rust/Cargo.toml -p symbol-dataset -- \
  fetch scripts/feature_cases/corpora.json

# 2. Mine cheap deterministic candidate packets.
cargo run --manifest-path rust/Cargo.toml -p symbol-dataset -- \
  mine scripts/feature_cases/corpora.json \
  --out .tmp/dataset/candidates.jsonl --limit-per-corpus 20

# 2b. Split the mined pool by a single primary op so batches do not overlap.
cargo run --manifest-path rust/Cargo.toml -p symbol-dataset -- \
  partition .tmp/dataset/candidates.jsonl \
  --out .tmp/dataset/partition/patch.jsonl --op patch

# 3. Ask Grok for strict single-operation task compositions, balanced by repo.
cargo run --manifest-path rust/Cargo.toml -p symbol-dataset -- \
  compose .tmp/dataset/partition/patch.jsonl \
  --out .tmp/dataset/composed.jsonl --model grok-4.5 \
  --limit-per-corpus 5

# 4. Reject plans that cannot deterministically reproduce child commits.
cargo run --manifest-path rust/Cargo.toml -p symbol-dataset -- \
  verify scripts/feature_cases/corpora.json .tmp/dataset/composed.jsonl \
  --symbol-bin rust/target/debug/symbol-rs \
  --out .tmp/dataset/verified.jsonl

# 5. Give only deterministic exact tasks to Grok. Rust mediates every model
# turn and executes only structured symbol-rs argv; no shell or file tools are
# available. Exact child files and undo are checked again.
cargo run --manifest-path rust/Cargo.toml -p symbol-dataset -- \
  exercise scripts/feature_cases/corpora.json .tmp/dataset/composed.jsonl \
  --symbol-bin rust/target/debug/symbol-rs --model grok-4.5 \
  --verified-input .tmp/dataset/verified.jsonl \
  --out .tmp/dataset/exercised.jsonl --keep-failures

# 6. Export exact actor traces as external-corpus harness scenarios.
cargo run --manifest-path rust/Cargo.toml -p symbol-dataset -- \
  export .tmp/dataset/exercised.jsonl \
  --out .tmp/dataset/harness.json \
  --corpora-manifest scripts/feature_cases/corpora.json

cargo run --manifest-path rust/Cargo.toml -p symbol-harness -- \
  .tmp/dataset/harness.json --repo-cache .tmp/corpora \
  --bin rust/target/debug/symbol-rs
```

The model is never the correctness oracle. Git child commits, exact changed
files, tracked-tree cleanliness, and undo determine acceptance. Composer
responses are cached by corpus, commit, model, and prompt version.

Supported primary writes are `patch`, `multi-patch`, `rename-symbol`,
`replace-symbol`, `insert-symbol`, and `delete-symbol`. `multi-patch` is one
atomic call containing multiple non-overlapping edits to one file.

For bulk generation, partition the mined candidates once per primary op and
compose each partition separately. That keeps commits unique across batches and
lets you scale patch-heavy and rare-op queues independently.
