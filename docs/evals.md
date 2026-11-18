# Evaluation report

KB `20261118-9f2b23d6`. Golden set: 147 questions (71 curated, 76 generated from the KB; see `evals/`).

## Deterministic gates (run in CI)

| Metric | Value | Gate |
|---|---|---|
| Router intent accuracy | 1.0 | pass |
| Language detection accuracy | 0.993 | — |
| Refusal correctness (answer vs refuse) | 0.953 | pass |
| False refusals / missed refusals | 6 / 0 | — |
| Retrieval hit@6 (all) | 0.965 | pass |
| Retrieval MRR (all) | 0.853 | pass |
| Input guardrail accuracy (25 EN/AR cases) | 1.0 | pass |

## Knowledge graph

75 entities and 157 evidence-backed edges (2 company, 27 place, 30 project, 2 person, 12 partner, 2 exchange). Retrieval runs BM25, dense and graph expansion and fuses them with reciprocal-rank fusion; the experiments below isolate the graph's effect.

## Retrieval by language

| Lang | n | hit@1 | hit@3 | hit@6 | MRR | nDCG@6 |
|---|---|---|---|---|---|---|
| en | 85 | 0.765 | 0.941 | 0.988 | 0.852 | 0.886 |
| ar | 29 | 0.828 | 0.897 | 0.897 | 0.856 | 0.867 |
| all | 114 | 0.781 | 0.93 | 0.965 | 0.853 | 0.881 |

## Retrieval experiments (all languages)

| Setting | hit@1 | hit@3 | hit@6 | MRR |
|---|---|---|---|---|
| bm25_only | 0.588 | 0.816 | 0.947 | 0.726 |
| dense_only | 0.658 | 0.781 | 0.851 | 0.729 |
| graph_only | 0.281 | 0.395 | 0.421 | 0.343 |
| hybrid_no_graph | 0.754 | 0.921 | 0.947 | 0.835 |
| hybrid+graph_0.2 | 0.781 | 0.93 | 0.965 | 0.853 |
| hybrid+graph_0.4 | 0.737 | 0.921 | 0.965 | 0.827 |
| hybrid+graph_1.0 | 0.649 | 0.912 | 0.974 | 0.776 |
