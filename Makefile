PY ?= python
VENV ?= .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
  BIN := $(VENV)/Scripts
endif
export PYTHONPATH := src
export PYTHONIOENCODING := utf-8
IMAGE ?= property-concierge
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
KB_VERSION := $(shell $(PY) -c "import json;print(json.load(open('data/kb/manifest.json'))['kb_version'])" 2>/dev/null || echo unknown)

.PHONY: bootstrap ingest kb run test lint eval eval-live docker docker-run smoke clean

bootstrap:            ## create the venv and install pinned dev dependencies
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -r requirements-ingest.lock pytest ruff

ingest:               ## crawl sources politely -> data/processed, then rebuild the KB snapshot
	$(BIN)/python -m concierge.scrape.run
	$(MAKE) kb

kb:                   ## rebuild data/kb (SQLite + FTS5 + embeddings) from data/processed/records.jsonl
	$(BIN)/python -m concierge.kb.build

run:                  ## serve on http://localhost:8000 (reads .env if present)
	$(BIN)/python -m uvicorn concierge.api.app:app --port 8000 --env-file .env

test:
	$(BIN)/python -m pytest -q

lint:
	$(BIN)/ruff check src tests evals scripts && $(BIN)/ruff format --check src tests evals scripts

eval:                 ## deterministic gates + retrieval experiments -> docs/evals.md
	$(BIN)/python evals/run_eval.py

eval-live:            ## end-to-end run against a deployment: make eval-live URL=https://...
	$(BIN)/python evals/run_eval.py --live $(URL)

docker:
	docker build --build-arg GIT_SHA=$(GIT_SHA) --build-arg KB_VERSION=$(KB_VERSION) -t $(IMAGE):$(GIT_SHA) -t $(IMAGE):latest .

docker-run:
	docker run --rm -p 8000:8000 --env-file .env --memory 512m --cpus 0.5 $(IMAGE):latest

smoke:                ## make smoke URL=https://your-deployment
	$(BIN)/python scripts/smoke.py $(URL)

clean:
	rm -rf .pytest_cache .ruff_cache data/runtime
