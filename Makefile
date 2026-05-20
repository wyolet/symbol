.DEFAULT_GOAL := help
.PHONY: help install sync test test-fast lint validate validate-specs audit clean

UV ?= uv

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
