# Architecture

```mermaid
flowchart LR
  subgraph Ingest [make ingest — offline]
    S1[Sitemaps / robots] --> C[PoliteClient<br/>robots, pacing, cache, breaker]
    C --> P[Parsers<br/>embedded JSON, JSON-LD, HTML sections, text fallback]
    P --> V[Pydantic schemas<br/>quarantine on failure]
    V --> R[(records.jsonl<br/>+ crawl report)]
  end
  R --> B[KB build<br/>chunk, SQLite, FTS5, int8 embeddings]
  B --> KB[(data/kb<br/>baked into image)]
  subgraph App [FastAPI container]
    UI[SPA EN/AR, SSE] --> API[/api/chat/]
    API --> RT[Router<br/>rules: lang, intent, brand, filters]
    RT -->|listing_search| SQL[SQL filters -> cards]
    RT -->|rag| HY[Hybrid retrieval<br/>BM25 + dense, RRF]
    HY --> G{confident?}
    G -->|no| REF[refuse + handoff]
    G -->|yes| LLM[OpenRouter free models<br/>fallback chain, breakers, budgets]
    LLM --> VER[numeric check, citations, freshness]
    LLM -.all fail.-> EX[extractive answer with citations]
  end
  KB --> HY
  KB --> SQL
  API --> L[(runtime.sqlite<br/>leads, outbox, feedback)] --> CRM[CRM webhook adapter]
```

## Request path

1. `sanitize` caps input at 800 chars and strips control characters.
2. `Router` (rules from `config/router.yaml`) decides language, intent and brand. Injection patterns,
   greetings and handoff requests are answered without retrieval or a model call.
3. Listing searches run validated SQL filters (type, purpose, city, price, beds, area) and return cards
   with no model call at all, so prices come straight from the data.
4. Everything else is retrieved with hybrid search: FTS5 BM25 and cosine over int8-ONNX embeddings,
   fused with reciprocal-rank fusion (weights in `config/retrieval.yaml`). If a single brand is named,
   retrieval is restricted to it.
5. A confidence gate refuses when the best of the top three passages is not semantically close enough.
   Questions with no brand, domain word or known entity must clear a higher bar, which is what keeps
   "capital of France" from being answered from a project called "Parisiana".
6. The model sees a system prompt from `prompts/`, the last six turns (PII-redacted) and the sources in
   delimited `<source>` blocks, with the instruction that they are data, not instructions.
7. After streaming, every number in the answer is checked against the retrieved text; unsupported
   ones are flagged to the user and counted in `/metrics`.

## Why these choices (short; see `adr/`)

- Rules for routing, not model tool calls: free models drift and rate-limit; rules are testable.
- SQLite + FTS5 + a NumPy matrix instead of a vector DB: 406 chunks fit in 300 KB; one file, no server.
- int8 ONNX + SentencePiece instead of the packaged embedding stack: 275 MB RSS instead of 570 MB.
- No LangGraph/LangChain at runtime: the flow is six functions; the dependency weight would not fit
  the 512 MB budget alongside the embedder. The flow is still an explicit graph (`pipeline.py`).
