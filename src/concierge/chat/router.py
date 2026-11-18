"""Deterministic language detection, intent routing, brand detection and listing-filter extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from concierge import config
from concierge.kb.store import ListingFilters
from concierge.scrape.extract import ARABIC_INDIC, detect_lang


@dataclass
class Route:
    intent: str  # injection | greeting | handoff | listing_search | rag
    lang: str
    brands: list[str] = field(default_factory=list)
    filters: ListingFilters | None = None


def _rx(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


def _contains(text: str, word: str) -> bool:
    if re.search(r"[؀-ۿ]", word):
        return word in text
    return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) is not None


class Router:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or config.load("router")
        self.injection = _rx(self.cfg["injection_patterns"])
        self.greeting = _rx(self.cfg["greeting_patterns"])
        self.handoff = _rx(self.cfg["handoff_patterns"])

    def brands(self, text: str) -> list[str]:
        low = text.lower()
        return [b for b, cues in self.cfg["brand_cues"].items() if any(_contains(low, c.lower()) for c in cues)]

    def route(self, text: str) -> Route:
        lang = detect_lang(text)
        low = text.lower().strip()
        if any(p.search(low) for p in self.injection):
            return Route("injection", lang)
        if any(p.search(low) for p in self.greeting):
            return Route("greeting", lang)
        brands = self.brands(low)
        if any(p.search(low) for p in self.handoff):
            return Route("handoff", lang, brands)
        filters = self.extract_filters(low)
        # Listing search only when the user describes a property to find, and is not asking about the
        # international developer's projects (those are answered from documents, not listings).
        if filters is not None and not set(brands) & set(self.cfg["document_only_brands"]):
            return Route("listing_search", lang, [self.cfg["listing_brand"]], filters)
        return Route("rag", lang, brands)

    # ---- filters -------------------------------------------------------------------------
    def extract_filters(self, low: str) -> ListingFilters | None:
        text = low.translate(ARABIC_INDIC)
        f = ListingFilters()
        for canon, words in self.cfg["listing_words"].items():
            if any(_contains(text, w) for w in words):
                f.property_type = canon
                break
        for purpose in ("daily_rent", "rent", "sale"):
            if any(_contains(text, w) for w in self.cfg["purpose_words"][purpose]):
                f.purpose = purpose
                break
        for city, aliases in self.cfg["city_aliases"].items():
            if any(_contains(text, a) for a in aliases):
                f.city = city
                break
        f.min_bedrooms, f.max_bedrooms = self._bedrooms(text)
        f.min_price, f.max_price = self._prices(text)
        m = re.search(r"(?:at least|min(?:imum)?|over|more than)\s*(\d[\d,]*)\s*(?:sqm|sq ?m|m2|square)", text)
        if m:
            f.min_area = float(m.group(1).replace(",", ""))
        hinted = any(_contains(text, h) for h in self.cfg["listing_hints"])
        has_type = f.property_type is not None
        if has_type and (f.city or f.purpose or hinted or f.max_price or f.min_bedrooms):
            return f
        if (f.city and (f.purpose or f.max_price or f.min_bedrooms)) or (f.purpose and (f.max_price or f.min_bedrooms)):
            return f
        return None

    def _bedrooms(self, text: str) -> tuple[int | None, int | None]:
        m = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)\s*(?:bed|br|bedroom)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d+)\s*\+?\s*-?\s*(?:bed|br\b|bedroom|غرف|غرفة|غرفه)", text)
        if m:
            n = int(m.group(1))
            return (n, None) if "+" in m.group(0) or "at least" in text else (n, n)
        return None, None

    def _amount(self, num: str, unit: str | None) -> float:
        val = float(num.replace(",", ""))
        unit = (unit or "").strip().lower()
        if unit in self.cfg["number_words"]["million"]:
            val *= 1_000_000
        elif unit in self.cfg["number_words"]["thousand"]:
            val *= 1_000
        return val

    def _prices(self, text: str) -> tuple[float | None, float | None]:
        units = "|".join(
            sorted({re.escape(u) for us in self.cfg["number_words"].values() for u in us}, key=len, reverse=True)
        )
        num = rf"(\d[\d,]*(?:\.\d+)?)\s*({units})?(?![a-z؀-ۿ])"
        m = re.search(rf"between\s*{num}\s*(?:and|-|to)\s*{num}", text)
        if m:
            return self._amount(m.group(1), m.group(2)), self._amount(m.group(3), m.group(4))
        m = re.search(
            rf"(?:under|below|less than|max(?:imum)?|up to|budget(?: of| is)?|within|أقل من|اقل من|حتى|بحدود|ميزانية)\s*(?:sar|sr|ريال)?\s*{num}",
            text,
        )
        if m:
            return None, self._amount(m.group(1), m.group(2))
        m = re.search(rf"(?:over|above|more than|at least|أكثر من|اكثر من)\s*(?:sar|sr|ريال)?\s*{num}", text)
        if m and not re.search(r"sqm|sq ?m|m2|متر", text[m.end() : m.end() + 8]):
            return self._amount(m.group(1), m.group(2)), None
        if re.search(r"(أقل من|اقل من|under|below)\s*(مليون|million)", text):
            return None, 1_000_000.0
        return None, None
