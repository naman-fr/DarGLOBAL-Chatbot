# 0002 Hybrid retrieval in SQLite

Date: 2026-11-18. Status: accepted.

**Decision.** FTS5 BM25 plus dense cosine over a NumPy matrix, fused with RRF (k=60), weights
bm25 1.0 / dense 1.2, top 6, behind the `KB` class. No vector database.

**Evidence** (`docs/evals.md`, 114 retrieval questions, 29 Arabic):

| Setting | hit@1 | hit@3 | MRR |
|---|---|---|---|
| BM25 only | 0.588 | 0.816 | 0.726 |
| Dense only | 0.658 | 0.781 | 0.729 |
| Hybrid 1 : 1.2 (chosen) | 0.754 | 0.921 | 0.835 |

BM25 alone cannot answer Arabic questions against English documents; dense alone misses exact names
and figures. Hybrid wins on every metric. 1 : 1 and 1 : 1.2 tie within noise; 1.2 keeps a small edge on
hit@1. A reranker was not added: the gain available above 0.92 hit@3 does not justify another ONNX model
in a 512 MB container.
