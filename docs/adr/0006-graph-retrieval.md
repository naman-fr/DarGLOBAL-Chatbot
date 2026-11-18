# 0006 Knowledge-graph retrieval (GraphRAG)

## Context

Hybrid retrieval (BM25 + dense) answers questions about one document well, but struggles with questions
that span documents ("which projects are in Oman?", "which projects carry a partner brand?") and with
Arabic questions over English sources when the key term is a proper noun. LLM-extracted GraphRAG
(entity and community summaries written by a model) needs many model calls at build time, which the
free tier's daily limits do not allow, and its edges cannot be traced back to a sentence.

## Decision

Build a small, deterministic entity graph at KB build time:

- Entities and EN/AR aliases are declared in `config/graph.yaml`.
- Co-mention in one sentence creates an edge labelled by the entity-type pair. Location and branding
  edges link only the nearest entity of the other type.
- Each edge stores up to six evidence chunk ids.

At query time, entities in the question seed a one-hop expansion. The chunks evidencing those edges form
a third ranked list in reciprocal-rank fusion. The relations whose evidence survives into the final
context go to the model with their source numbers and are shown to the user.

## Results (docs/evals.md)

The graph weight was chosen by a sweep. At weight 0.2, hybrid + graph improves every metric over hybrid
alone:

| Metric | Hybrid alone | Hybrid + graph (0.2) |
|---|---|---|
| hit@1 | 0.754 | 0.781 |
| hit@6 | 0.947 | 0.965 |
| MRR | 0.835 | 0.853 |

Higher weights raise recall further, but they push neighbour documents above the exact match. The golden
set rewards one expected page, so it understates the graph's value for aggregation questions.

## Consequences

- Recall depends on the entity list. A new project needs one line in `config/graph.yaml`.
- Co-mention is not causation. Edges are labels for navigation, and the model is told to state only
  what the source text supports.
