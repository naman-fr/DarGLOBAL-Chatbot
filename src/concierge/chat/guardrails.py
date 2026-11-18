"""Input and output guardrails.

Input rails (before routing):
  normalise   NFKC + strip zero-width / bidi control characters, so obfuscated text routes like plain text
  encoded     long base64 / hex payloads are treated as injection attempts
  unsafe      a narrow deny-list (weapons, self-harm, laundering, intrusion) gets a fixed refusal
  advice      personal investment-advice requests are answered factually, with a standing disclaimer

Output rails (after generation, before citations are sent):
  citations   [n] markers that point at no source are removed; citation coverage is measured
  grounding   each factual sentence is embedded and compared with the passages it cites
  numbers     figures that never appear in the sources are flagged (see guard.unsupported_numbers)
  leakage     a long run of words copied from the system prompt replaces the answer with a refusal
  pii         emails / phone numbers that are not in the sources are redacted
  blending    one sentence citing two different companies is flagged
  advice      "guaranteed returns"-style language is flagged and the disclaimer shown
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

import numpy as np

from concierge import config
from concierge.chat import guard

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_B64 = re.compile(r"[A-Za-z0-9+/=]{%d,}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2}[\s:]?){%d,}")
_CITE = re.compile(r"\[(\d+)\]")
_SENT = re.compile(r"(?<=[.!?؟])\s+|\n+")
_WORD = re.compile(r"[\w؀-ۿ]+")


def _cfg() -> dict:
    return config.load("guardrails")


def _rx(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


# ---- input --------------------------------------------------------------------------------------
def normalise(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))


@dataclass
class InputVerdict:
    block: str | None = None  # "unsafe" | "encoded" | None
    advice: bool = False


def check_input(text: str) -> InputVerdict:
    cfg = _cfg()["input"]
    n = cfg["encoded_min_chars"]
    if re.search(_B64.pattern % n, text) or re.search(_HEX.pattern % (n // 2), text):
        return InputVerdict(block="encoded")
    low = text.lower()
    if any(p.search(low) for p in _rx(cfg["unsafe_patterns"])):
        return InputVerdict(block="unsafe")
    return InputVerdict(advice=any(p.search(low) for p in _rx(cfg["advice_patterns"])))


# ---- output -------------------------------------------------------------------------------------
@dataclass
class Check:
    id: str
    status: str  # pass | warn | fail
    detail: str = ""


@dataclass
class OutputReport:
    answer: str
    replaced: bool = False
    checks: list[Check] = field(default_factory=list)
    grounding: dict = field(default_factory=dict)
    unsupported_numbers: list[str] = field(default_factory=list)
    advice: bool = False

    @property
    def status(self) -> str:
        states = {c.status for c in self.checks}
        return "fail" if "fail" in states else "warn" if "warn" in states else "pass"

    def to_event(self) -> dict:
        return {
            "status": self.status,
            "checks": [asdict(c) for c in self.checks],
            "grounding": self.grounding,
        }


def _sentences(answer: str, min_words: int) -> list[str]:
    out = []
    for s in _SENT.split(answer):
        s = s.strip().lstrip("-*• ").strip()
        if len(_WORD.findall(_CITE.sub("", s))) >= min_words:
            out.append(s)
    return out


def _content_words(text: str, stop: set[str]) -> set[str]:
    words = (w.lower() for w in _WORD.findall(_CITE.sub("", text.replace("**", ""))))
    return {w for w in words if (len(w) >= 4 or w.isdigit()) and w not in stop}


def lexical_support(sentence: str, passage: str, stop: set[str]) -> float:
    """Share of the sentence's content words (names, figures, key terms) found in the passage."""
    want = _content_words(sentence, stop)
    if not want:
        return 0.0
    have = _content_words(passage, stop)
    return len(want & have) / len(want)


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _WORD.findall(text)]
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def check_output(
    answer: str,
    hits: list,
    sources_text: str,
    system_prompt: str,
    embed=None,
    hit_vectors: np.ndarray | None = None,
    min_digits: int = 3,
) -> OutputReport:
    """hits: the retrieved passages in source-number order (1-based in the answer).
    embed: callable(list[str]) -> np.ndarray of unit vectors, or None to skip grounding.
    hit_vectors: unit vectors for the hits, same order."""
    cfg = _cfg()["output"]
    rep = OutputReport(answer=answer)

    # leakage first: if it trips, nothing else matters
    n = cfg["leak_ngram_words"]
    if system_prompt and _ngrams(answer, n) & _ngrams(system_prompt, n):
        rep.replaced = True
        rep.checks.append(Check("leakage", "fail", "answer repeated the system prompt"))
        return rep
    rep.checks.append(Check("leakage", "pass"))

    # citations point at real sources
    valid = set(range(1, len(hits) + 1))
    bad = sorted({int(m) for m in _CITE.findall(answer)} - valid)
    if bad:
        rep.answer = _CITE.sub(lambda m: m.group(0) if int(m.group(1)) in valid else "", rep.answer)
    sentences = _sentences(rep.answer, cfg["grounding_min_words"])[: cfg["grounding_max_sentences"]]
    cited = [s for s in sentences if _CITE.search(s)]
    coverage = len(cited) / len(sentences) if sentences else 1.0
    status = "warn" if bad or coverage < cfg["citation_pass_ratio"] else "pass"
    detail = f"{len(cited)}/{len(sentences)} sentences cited"
    if bad:
        detail += f"; removed invalid markers {bad}"
    rep.checks.append(Check("citations", status, detail))

    # Sentence-level grounding against the cited passages. A sentence is supported when most of its
    # content words appear in a cited passage (same-language answers), or when it is semantically close
    # to one (cross-language answers, paraphrase).
    if len(hits) and sentences:
        stop = set(cfg.get("grounding_stopwords", []))
        sims = None
        if embed is not None and hit_vectors is not None:
            sims = embed([_CITE.sub("", s) for s in sentences]) @ hit_vectors.T  # sentences x hits
        supported = 0
        per = []
        miscited = 0
        for i, s in enumerate(sentences):
            nums = [int(m) - 1 for m in _CITE.findall(s) if int(m) in valid]
            everything = list(range(len(hits)))

            def support(idx: list[int], i: int = i, s: str = s) -> tuple[float, float]:
                lex = max(lexical_support(s, hits[j].text, stop) for j in idx)
                dense = float(sims[i, idx].max()) if sims is not None else 0.0
                return lex, dense

            lex, dense = support(nums or everything)
            ok = lex >= cfg["grounding_min_lexical"] or dense >= cfg["grounding_min_similarity"]
            if not ok and nums:  # supported by a passage, but not the one it cites
                lex2, dense2 = support(everything)
                if lex2 >= cfg["grounding_min_lexical"] or dense2 >= cfg["grounding_min_similarity"]:
                    ok, lex, dense = True, lex2, dense2
                    miscited += 1
            supported += ok
            per.append({"lexical": round(lex, 2), "semantic": round(dense, 3), "supported": bool(ok)})
        ratio = supported / len(sentences)
        rep.grounding = {
            "supported": supported,
            "total": len(sentences),
            "score": round(ratio, 3),
            "miscited": miscited,
            "per_sentence": per,
        }
        detail = f"{supported}/{len(sentences)} sentences supported"
        if miscited:
            detail += f" ({miscited} by a different source than cited)"
        rep.checks.append(Check("grounding", "pass" if ratio >= cfg["grounding_pass_ratio"] else "warn", detail))

    # numbers
    rep.unsupported_numbers = guard.unsupported_numbers(rep.answer, sources_text, min_digits)
    rep.checks.append(
        Check("numbers", "warn" if rep.unsupported_numbers else "pass", ", ".join(rep.unsupported_numbers))
    )

    # PII the sources do not contain
    leaked = []
    for rx in (guard._EMAIL, guard._PHONE):
        for m in rx.findall(rep.answer):
            if m not in sources_text:
                leaked.append(m)
    if leaked:
        rep.answer = guard.redact(rep.answer)
    rep.checks.append(Check("pii", "warn" if leaked else "pass", f"redacted {len(leaked)}" if leaked else ""))

    # one sentence, two companies
    blended = 0
    for s in sentences:
        brands = {hits[int(m) - 1].brand for m in _CITE.findall(s) if int(m) in valid}
        blended += len(brands) > 1
    rep.checks.append(
        Check("brand_separation", "warn" if blended else "pass", f"{blended} mixed sentences" if blended else "")
    )

    # advice language
    rep.advice = any(p.search(rep.answer.lower()) for p in _rx(cfg["advice_patterns"]))
    rep.checks.append(Check("advice", "warn" if rep.advice else "pass"))
    return rep
