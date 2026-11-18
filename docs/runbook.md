# Runbook

## Free model outage or rate limiting
Symptom: answers start with "The AI model is unavailable right now", `/readyz` shows `last_ok_model`
stale, `concierge_degraded_answers_total` rises. The bot keeps working in extractive mode.
1. `GET /api/admin/status` (Bearer `ADMIN_TOKEN`) → `llm.last_error`, `llm.benched`.
2. 429 on every model: daily free quota hit. Wait for the reset, or add $10 credit to the OpenRouter
   account (raises the free-model daily limit), or lower `budget.global_requests_per_day`.
3. A model was removed: discovery drops it at the next refresh; restart to force it.

## Scraper blocked or broken
1. `data/processed/crawl_report.json` → `blocked`, `per_host`. Blocked means a challenge page; do not
   try to get round it. Ask the site for access (allow-listed UA, API, export).
2. Parser broke (records drop, quarantine grows): save the page as a fixture under `tests/fixtures/`,
   write a failing test, fix the parser, run `make ingest`.

## Cold start
Free instances sleep. First request after idle takes 30–60 s; the UI shows a waking state. An external
uptime ping every 10 minutes keeps it warm if the host's terms allow it.

## KB rollback
The KB snapshot is part of the image. Redeploy the previous image tag, or `git revert` the commit that
changed `data/processed/records.jsonl` and push.

## Lead data deletion (data-subject request)
```
docker exec <container> python -c "from concierge.api.runtime_store import RuntimeStore; print(RuntimeStore().delete_lead('<email or phone>'))"
```
Leads and feedback are purged automatically after 90 / 180 days (`config/app.yaml`).

## SLOs and next dashboards
- Availability of `/healthz` 99% (free tier sleeps; measured on awake time).
- p95 time-to-first-token under 6 s, p95 full answer under 15 s.
- Refusal rate under 25% of on-topic questions; unverified-number rate under 5%.
Next dashboards: latency histogram by model; fallback and degraded rates; top unanswered questions
(from `/api/admin/status`).
