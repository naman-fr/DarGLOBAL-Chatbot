# Model card

**Generator.** Whatever free OpenRouter model is first healthy in the preference list
(`config/llm.yaml`; on 2026-11-18 Gemma 4 31B/26B, then Nemotron 3 Ultra, ERNIE, Ling, with
`openrouter/free` last). The served model is shown under every answer.

**Embedder.** paraphrase-multilingual-MiniLM-L12-v2 (50+ languages, 384-d), int8 ONNX. Used for
retrieval only.

**Intended use.** Answering questions about two named real-estate brands from their public
information, with citations, and searching a sample of public listings.

**Not for.** Investment, legal, tax or mortgage advice; valuations; availability or price commitments.

**Known failure modes.**
- Free models sometimes ignore the citation instruction or paraphrase a figure; the numeric check
  flags numbers that do not appear in the sources but cannot catch a wrong label on a right number.
- Arabic answers depend on the generator's Arabic; retrieval for Arabic is weaker at hit@6
  (0.90 vs 0.97 English) because most sources are English.
- Rules-based routing misses unusual phrasings of listing searches; those fall through to document
  answers, which then refuse if the KB has nothing relevant.
