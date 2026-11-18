"""Input hygiene, PII redaction before any LLM call, and numeric-claim verification."""

from __future__ import annotations

import re

from concierge.scrape.extract import ARABIC_INDIC

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+|00)?\d[\d\s\-()]{7,}\d(?!\d)")
_ID = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")  # 10-digit national / iqama style ids
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|[kKmMbB](?:n|illion)?\b)?")


def sanitize(text: str, max_chars: int) -> str:
    return _CTRL.sub("", text or "").strip()[:max_chars]


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _IBAN.sub("[iban]", text)
    text = _ID.sub("[id]", text)
    return _PHONE.sub("[phone]", text)


def _norm(num: str) -> str:
    n = num.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def numbers_in(text: str, min_digits: int) -> set[str]:
    text = text.translate(ARABIC_INDIC)
    text = re.sub(r"\[\d+\]", " ", text)  # citation markers are not claims
    out = set()
    for m in _NUM.finditer(text):
        n = _norm(m.group(1))
        if len(n.replace(".", "")) >= min_digits or m.group(2):
            out.add(n)
    return out


def unsupported_numbers(answer: str, context: str, min_digits: int) -> list[str]:
    """Numbers in the answer that never appear in the retrieved context.

    Years and round figures are checked too: an unsupported '2027' handover date is exactly the
    kind of claim this exists to catch.
    """
    ctx = numbers_in(context, 1)
    ctx_text = context.translate(ARABIC_INDIC).replace(",", "")
    missing = []
    for n in sorted(numbers_in(answer, min_digits)):
        if n in ctx or n in ctx_text:
            continue
        missing.append(n)
    return missing
