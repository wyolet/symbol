#!/usr/bin/env zsh
# Survey: clone target repos and run ca audit + loc, saving output to survey/runs/
# Usage: ./scripts/survey.sh [name...]   -- run only named repos from the list
#        ./scripts/survey.sh             -- run all repos

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="$SCRIPT_DIR/.."
REPOS_DIR="$ROOT/survey/repos"
RUNS_DIR="$ROOT/survey/runs"

mkdir -p "$REPOS_DIR" "$RUNS_DIR"

# ---------------------------------------------------------------------------
# Repo definitions: name slug type includes excludes
# Multiple globs separated by commas. Empty = no flag passed.
# ---------------------------------------------------------------------------

typeset -A REPO_SLUGS REPO_TYPES REPO_INCLUDES REPO_EXCLUDES

define_repo() {
    local name="$1" slug="$2" type="$3" includes="$4" excludes="$5"
    REPO_SLUGS[$name]="$slug"
    REPO_TYPES[$name]="$type"
    REPO_INCLUDES[$name]="$includes"
    REPO_EXCLUDES[$name]="$excludes"
}

# --- libs ---
define_repo fastapi \
    "tiangolo/fastapi" lib \
    "fastapi/**,tests/**" \
    "docs/**,scripts/**"

define_repo sqlmodel \
    "tiangolo/sqlmodel" lib \
    "sqlmodel/**,sqlmodel_slim/**,tests/**" \
    "docs/**,data/**"

define_repo httpx \
    "encode/httpx" lib \
    "httpx/**,tests/**" \
    "docs/**,scripts/**"

define_repo rich \
    "Textualize/rich" lib \
    "rich/**,tests/**" \
    "docs/**"

define_repo click \
    "pallets/click" lib \
    "src/**,tests/**" \
    "docs/**,examples/**"

define_repo cookiecutter \
    "cookiecutter/cookiecutter" lib \
    "cookiecutter/**,tests/**" \
    "docs/**,logo/**"

# --- apps ---
define_repo full-stack-fastapi-template \
    "tiangolo/full-stack-fastapi-template" app \
    "backend/app/**,backend/tests/**" \
    "frontend/**,img/**,backend/app/email-templates/**"

define_repo healthchecks \
    "healthchecks/healthchecks" app \
    "hc/**" \
    "static/**,templates/**,docker/**,stuff/**"

define_repo searxng \
    "searxng/searxng" app \
    "searx/**,searxng_extra/**,tests/**" \
    "client/**,docs/**,utils/**,container/**"

define_repo saleor \
    "saleor/saleor" app \
    "saleor/**" \
    "templates/**,scripts/**,deployment/**,.semgrep/**"

# ---------------------------------------------------------------------------

ALL_NAMES=(fastapi sqlmodel httpx rich click cookiecutter full-stack-fastapi-template healthchecks searxng saleor)

if [[ $# -gt 0 ]]; then
    TARGETS=("$@")
else
    TARGETS=("${ALL_NAMES[@]}")
fi

build_flags() {
    local includes="$1" excludes="$2"
    local flags=()
    local parts
    IFS=',' read -rA parts <<< "$includes"
    for g in "${parts[@]}"; do
        [[ -n "$g" ]] && flags+=(--include "$g")
    done
    IFS=',' read -rA parts <<< "$excludes"
    for g in "${parts[@]}"; do
        [[ -n "$g" ]] && flags+=(--exclude "$g")
    done
    echo "${flags[@]:-}"
}

for name in "${TARGETS[@]}"; do
    slug="${REPO_SLUGS[$name]:-}"
    if [[ -z "$slug" ]]; then
        echo "ERROR: unknown repo '$name'" >&2
        exit 1
    fi

    type="${REPO_TYPES[$name]}"
    includes="${REPO_INCLUDES[$name]}"
    excludes="${REPO_EXCLUDES[$name]}"
    repo_path="$REPOS_DIR/$name"

    echo "=== $slug  [$type] ==="

    if [[ -d "$repo_path/.git" ]]; then
        echo "  already cloned, skipping"
    else
        echo "  cloning (depth=1)..."
        git clone --depth=1 "https://github.com/$slug.git" "$repo_path"
    fi

    local_flags=()
    IFS=' ' read -rA local_flags <<< "$(build_flags "$includes" "$excludes")"

    echo "  include: $includes"
    echo "  exclude: $excludes"

    echo "  running ca loc..."
    uv run --project "$ROOT" ca loc "$repo_path" \
        > "$RUNS_DIR/$name-loc.txt" 2>&1 || true

    echo "  running ca audit (rich)..."
    uv run --project "$ROOT" ca audit "$repo_path" "${local_flags[@]}" \
        > "$RUNS_DIR/$name-audit.txt" 2>&1 || true

    echo "  running ca audit (json)..."
    uv run --project "$ROOT" ca audit "$repo_path" "${local_flags[@]}" --format json \
        > "$RUNS_DIR/$name-audit.json" 2>&1 || true

    echo "  saved -> survey/runs/$name-{loc,audit}.txt + audit.json"
    echo
done

echo "All done. Results in survey/runs/"
