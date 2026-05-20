.DEFAULT_GOAL := help
.PHONY: help install sync test test-fast lint validate validate-specs audit clean \
        build-go-scan build-go-scan-all

UV ?= uv
GO ?= go
GO_SCAN_DIR := src/wyolet/symbol/adapters/go_ast/daemon
BIN_DIR := src/wyolet/symbol/bin
# Cross-compile targets shipped in wheels. Keep this list in sync with
# the CI workflow under .github/workflows/ci.yml.
GO_SCAN_TARGETS := \
  darwin-arm64 \
  darwin-amd64 \
  linux-arm64 \
  linux-amd64 \
  windows-amd64

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Sync dependencies including dev extras (alias for `sync`)
	$(UV) sync --all-extras

sync: install ## Alias for install

test: ## Run the full pytest suite
	$(UV) run --extra dev pytest -q

test-fast: ## Run pytest with -x (fail fast) and verbose names
	$(UV) run --extra dev pytest -x -v

lint: ## Run ruff
	$(UV) run --extra dev ruff check .

validate: validate-specs ## Validate JSON schemas + all bundled specs (alias for validate-specs)

validate-specs: ## Validate every data/specs/*/spec.toml against schemas/symbol.spec.schema.json
	$(UV) run --extra dev pytest tests/test_spec_schema.py tests/test_config_schema.py -v

audit: ## Run `symbol audit` on this repo (dogfood)
	$(UV) run symbol audit

clean: ## Remove caches
	rm -rf .pytest_cache .ruff_cache **/__pycache__ .symbol/transactions

build-go-scan: ## Build go-scan daemon for the current platform (dev convenience)
	cd $(GO_SCAN_DIR) && $(GO) build -o go-scan .

build-go-scan-all: ## Cross-compile go-scan for every shipped target → src/wyolet/symbol/bin/
	@mkdir -p $(BIN_DIR)
	@for target in $(GO_SCAN_TARGETS); do \
	  GOOS=$${target%-*} GOARCH=$${target#*-} \
	    suffix=$$([ "$${target%-*}" = "windows" ] && echo .exe || echo ""); \
	  echo "→ $(BIN_DIR)/go-scan-$$target$$suffix"; \
	  GOOS=$${target%-*} GOARCH=$${target#*-} \
	    $(GO) -C $(GO_SCAN_DIR) build -o ../../../../../../$(BIN_DIR)/go-scan-$$target$$suffix .; \
	done
