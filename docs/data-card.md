# Data card: KB `20261118-9f2b23d6`

| | |
|---|---|
| Records | 146 (DarGlobal 109, parent company 37, Wasalt 0) |
| Kinds | 110 articles (RNS sections, encyclopaedia sections), 36 corporate pages |
| Languages | 143 English, 3 Arabic records; Arabic questions are served by cross-lingual embeddings |
| Chunks indexed | 406 (900 chars, 150 overlap, paragraph-packed) |
| Embeddings | paraphrase-multilingual-MiniLM-L12-v2, int8 ONNX, 384-d, float16 on disk |
| Fetched | 2026-11-18, 26.7 s crawl, 175 requests, 0 quarantined records |
| Size | 1.5 MB SQLite + vectors, baked into the image |

## Coverage and bias

- **DarGlobal** is described mostly through investor disclosures. Answers lean towards results,
  strategy, launches and project status, and away from unit-level marketing (floor plans, prices,
  payment plans), which the blocked official site holds.
- **Parent company** pages describe Saudi projects; they are kept as context and labelled.
- **Wasalt**: none in this snapshot (see `data-sources.md`). When access works, listings are sampled
  by stratifying city × category from the sitemap (10 cities × 9 categories, round-robin so each city is
  covered before any gets depth, 70 English + 20 Arabic result pages ≈ 2 800 listings). That sample is
  biased towards whatever each result page ranks first (promoted and recently updated listings) and
  under-represents small cities.

## Freshness

Every answer carries the fetch date. Content older than 120 days (`stale_after_days`) triggers a
"may have changed" note. Listings are volatile; cards always link to the live listing.

## Personal data

None stored from sources. Leads typed into the demo form live in a separate runtime database with a
90-day retention purge, never in the KB and never sent to the model.
