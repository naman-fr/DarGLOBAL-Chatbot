# 0007 Guardrails as code

## Context

Free models vary in quality and change without notice. The assignment requires grounded answers with
citations, and the master prompt requires refusals, no investment advice, privacy, and no blending of
companies. Calling a second model as a judge would double latency and spend the free daily budget.

## Decision

Deterministic rails in `src/concierge/chat/guardrails.py`, configured in `config/guardrails.yaml`.

Input rails:
- NFKC and invisible-character normalisation.
- Encoded-payload detection.
- A narrow unsafe list.
- Advice-request detection.

Output rails:
- Citation validity and coverage.
- Sentence grounding: content-word support, or multilingual semantic similarity for cross-language
  answers.
- Unsupported numbers.
- System-prompt leakage.
- Unsourced contact details.
- Brand blending.
- Advice language.

Every check is reported to the UI ("Answer checks") and to the step trace.

## Calibration

Thresholds were set on real answers and on deliberately fabricated sentences against the same
passages:

| Sentences | Lexical support | Semantic similarity |
|---|---|---|
| Fabricated | ≤ 0.27 | ≤ 0.24 |
| Supported | 0.57–1.0, or semantic ≥ 0.45 | |

A sentence that its cited passage does not support, but another retrieved passage does, counts as
supported and is reported as mis-cited.

The input rails have a 25-case EN/AR suite (`evals/guardrail_cases.jsonl`) gated in CI at 0.90. It
currently scores 1.0.

## Consequences

- Rules miss paraphrased attacks that a classifier would catch. The rails sit behind the system prompt's
  own rules, not instead of them.
- Grounding is a support signal, not proof. A warning tells the reader to check the cited source; it
  does not hide the answer.
