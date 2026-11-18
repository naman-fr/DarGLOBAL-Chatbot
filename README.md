# Property Concierge

A bilingual (English/Arabic) chatbot that answers questions about two related real-estate brands
(DarGlobal, the London-listed international developer, and Wasalt, the Saudi property marketplace)
from data scraped from public sources, with a citation and "as of" date on every answer.

**Live URL (temporary):** https://treasure-region-preventing-officer.trycloudflare.com. This is a Cloudflare
quick tunnel to the container on the build machine, up while that machine is on. The permanent Render URL
replaces it once the service is created (see "Deployment").

Independent technical demo, not affiliated with either company.

## What it does

- Answers from a knowledge base built by a polite crawler. Brand labels on every answer, and no
  blending: parent-company content is labelled as such.
- Hybrid retrieval (SQLite FTS5 BM25 + multilingual embeddings, RRF). Arabic questions retrieve
  English sources through cross-lingual embeddings.
- Generates with free OpenRouter models through a fallback chain. If all of them fail, it returns the
  cited passages instead of an error.
- Refuses when the KB doesn't know, offers a human handoff and lead form (with consent), and handles
  prompt injection and off-topic questions.
- Listing search ("3-bed apartments for sale in Riyadh under 1m", "فيلا للإيجار في جدة") runs as SQL
  over structured listing data with no model in the loop.
- Flags any number in an answer that does not appear in the cited sources.

## Five-minute local run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-ingest.lock pytest ruff
cp .env.example .env              # add OPENROUTER_API_KEY for generated answers (optional)
make kb                           # builds data/kb from the committed records snapshot
make run                          # http://localhost:8000
```

Or with Docker (no Python needed):

```bash
docker build -t property-concierge . && docker run --rm -p 8000:8000 --env-file .env --memory 512m property-concierge
```

`make test`, `make lint`, `make eval`, `make ingest` (re-crawl), `make smoke URL=...`.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the diagram and request path. In short:
`scrape/` → `records.jsonl` → `kb/build.py` → `data/kb` (baked into the image) → `api/app.py`
(FastAPI, SSE) → `chat/pipeline.py` (router → SQL or retrieval → gate → LLM → verify).

## Configuration

Everything tunable is in `config/` (sources, crawler politeness, retrieval, LLM preferences and
budgets, routing vocabulary, eval gates, theme), prompts in `prompts/`, and UI strings in `locales/`.
A test fails if a brand name, site URL or model ID appears in `src/`.

| Env var | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Generated answers. Without it the bot answers extractively. |
| `ADMIN_TOKEN` | Enables `GET /api/admin/status`. |
| `APP_URL` | Attribution header sent to OpenRouter. |
| `CRM_WEBHOOK_URL` | Optional lead webhook; otherwise a stub adapter logs. |

## Data

See [docs/data-sources.md](docs/data-sources.md) and [docs/data-card.md](docs/data-card.md).
Short version: the DarGlobal site and, after a few requests, the Wasalt site put automated clients
behind bot challenges. The crawler respects that and does not work around it. DarGlobal is covered from
its own regulatory announcements, Wikipedia and the parent company's site (146 records, 406 chunks).
Wasalt listing search is built and tested, but this snapshot has no Wasalt rows. The bot says so.

## Evaluation

`make eval` runs the deterministic gates used in CI. Latest results ([docs/evals.md](docs/evals.md)):

| Metric | Value |
|---|---|
| Golden set | 147 questions (71 curated EN/AR, 76 generated from the KB) |
| Router intent accuracy | 1.00 |
| Retrieval hit@3 / hit@6 / MRR | 0.92 / 0.95 / 0.84 (Arabic: 0.90 / 0.90 / 0.84) |
| Refusal correctness | 0.95 (0 missed refusals) |

`make eval-live URL=...` runs curated questions end to end against a deployment and adds citation
rate, unverified-number rate, served models and latency to the report.

## OpenRouter free tier

Free (`:free`) models need no card. Limits as documented at the time of writing: 20 requests per
minute, and 50 requests per day for accounts with less than $10 of credit (1 000/day with $10+). The
app's budgets (`config/llm.yaml`) sit below those, listing searches use no model calls, and greetings,
handoffs and refusals are answered without the model.

## Deployment

Render, Docker runtime, `render.yaml` blueprint, free plan (512 MB). Measured in a 512 MB / 0.5 CPU
container: image 688 MB, steady RSS 275 MB, peak 358 MB. The UI shows a "waking up" state while a
sleeping free instance starts. CI (`.github/workflows/ci.yml`) runs lint, tests and eval gates, builds
and scans the image, and runs `scripts/smoke.py` against the live URL.

## Limitations and next steps

- Wasalt data is missing until the operator gets crawler access; then run `make ingest`.
- DarGlobal brochure-level details (prices, payment plans) live on the blocked site and are absent.
- Routing is rules-based. Unusual listing phrasings fall through to document answers.
- Not built yet: LangSmith tracing and MLflow experiment tracking (eval results are JSON and Markdown
  in `docs/`), calculators, project comparison, voice input, a reranker.
