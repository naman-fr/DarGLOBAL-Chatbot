# Evaluation report

KB `20261118-9f2b23d6`. Golden set: 147 questions (71 curated, 76 generated from the KB; see `evals/`).

## Deterministic gates (run in CI)

| Metric | Value | Gate |
|---|---|---|
| Router intent accuracy | 1.0 | pass |
| Language detection accuracy | 0.993 | — |
| Refusal correctness (answer vs refuse) | 0.953 | pass |
| False refusals / missed refusals | 6 / 0 | — |
| Retrieval hit@6 (all) | 0.947 | pass |
| Retrieval MRR (all) | 0.835 | pass |

## Retrieval by language

| Lang | n | hit@1 | hit@3 | hit@6 | MRR | nDCG@6 |
|---|---|---|---|---|---|---|
| en | 85 | 0.741 | 0.929 | 0.965 | 0.833 | 0.866 |
| ar | 29 | 0.793 | 0.897 | 0.897 | 0.839 | 0.854 |
| all | 114 | 0.754 | 0.921 | 0.947 | 0.835 | 0.863 |

## Retrieval experiments (all languages)

| Setting | hit@1 | hit@3 | hit@6 | MRR |
|---|---|---|---|---|
| bm25_only | 0.588 | 0.816 | 0.947 | 0.726 |
| dense_only | 0.658 | 0.781 | 0.851 | 0.729 |
| hybrid_1_1 | 0.746 | 0.921 | 0.965 | 0.835 |
| hybrid_1_1.2 | 0.754 | 0.921 | 0.947 | 0.835 |
| hybrid_1_2 | 0.746 | 0.921 | 0.947 | 0.826 |

## Live run

Target: `https://treasure-region-preventing-officer.trycloudflare.com`, 12 questions.

| Metric | Value |
|---|---|
| answered_with_citation | 1.0 |
| refusal_correct | 1.0 |
| unverified_number_rate | 0.0 |
| llm_served_rate | 0.917 |
| latency_p50_s | 12.32 |
| latency_p95_s | 51.59 |
| models served | dots-studio/dots-3-note-preview:free, extractive-fallback, nvidia/nemotron-3-ultra-550b-a55b:free |
