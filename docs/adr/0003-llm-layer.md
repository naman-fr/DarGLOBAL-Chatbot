# 0003 Free OpenRouter models without trusting them

Date: 2026-11-18. Status: accepted.

- Models are discovered from `GET /api/v1/models` at boot and daily: zero prompt and completion price,
  context ≥ 32k, text output, minus a deny list (music, code, tiny, long-reasoning models). Preference
  order is regexes in `config/llm.yaml`, so a renamed model does not need a code change.
- Each request sends a `models` array of three (OpenRouter's server-side fallback); the client retries
  the next group on 429/5xx/empty output with backoff, benches a model after two failures for five
  minutes, and ends with `openrouter/free`. The served model is recorded and shown in the UI.
- Routing and listing filters never depend on the model. If every model fails, the user gets an
  extractive answer built from the cited passages.
- Budgets: 900 requests/day globally and 30/hour per session (config), under OpenRouter's free limits.
- Only the question, six redacted turns and public KB text are sent; emails, phone numbers and ID
  numbers are redacted first, because free endpoints may retain prompts.
