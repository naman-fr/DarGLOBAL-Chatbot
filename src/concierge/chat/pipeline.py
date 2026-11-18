"""The chat flow as an explicit graph of small steps.

    sanitize -> detect language -> route --+-- injection / greeting / handoff -> canned reply
                                           +-- listing_search -> SQL filters -> cards (no LLM)
                                           +-- rag -> retrieve -> confident? -- no -> refuse + handoff
                                                                      | yes
                                                                      v
                                                      generate (LLM, streamed) -> verify numbers -> citations
                                                      (LLM down -> extractive answer with citations)

Every step emits events; the API turns them into SSE.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from concierge import config
from concierge.chat import guard
from concierge.chat.llm import LLMUnavailable, OpenRouter
from concierge.chat.router import Route, Router
from concierge.kb.store import KB, Hit
from concierge.observability import log, metrics


@dataclass
class Turn:
    role: str
    content: str


@dataclass
class ChatRequest:
    message: str
    session_id: str
    history: list[Turn] = field(default_factory=list)
    lang: str | None = None  # UI language; the message language wins when they differ


def brand_name(brand: str, lang: str) -> str:
    b = config.load("sources")["brands"].get(brand, {})
    return (b.get("name_ar") if lang == "ar" else None) or b.get("name") or brand


def target_brands() -> list[str]:
    return [k for k, v in config.load("sources")["brands"].items() if v.get("role") != "parent"]


def _fmt_money(v: float | None) -> str:
    return "" if v is None else f"{v:,.0f}"


def _age_days(iso: str | None) -> float:
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return (datetime.now(UTC) - dt).total_seconds() / 86400


class Concierge:
    def __init__(self, kb: KB, llm: OpenRouter | None = None, router: Router | None = None) -> None:
        self.kb = kb
        self.llm = llm or OpenRouter()
        self.router = router or Router()
        self.app = config.load("app")
        self.rcfg = config.load("retrieval")

    # ---- entry -------------------------------------------------------------------------------
    async def run(self, req: ChatRequest) -> AsyncIterator[dict]:
        started = time.monotonic()
        text = guard.sanitize(req.message, self.app["max_input_chars"])
        route = self.router.route(text)
        lang = route.lang if text else (req.lang or "en")
        L = config.locale(lang)
        metrics.REQUESTS.labels(intent=route.intent, lang=lang).inc()
        yield {
            "event": "meta",
            "data": {
                "intent": route.intent,
                "lang": lang,
                "dir": L["dir"],
                "brands": route.brands,
                "kb_version": self.kb.version,
            },
        }
        try:
            if route.intent == "injection":
                metrics.REFUSALS.labels(reason="injection").inc()
                yield self._say(L["refuse_injection"])
            elif route.intent == "greeting":
                names = " & ".join(brand_name(b, lang) for b in target_brands())
                yield self._say(L["greeting"].format(brand_list=names))
            elif route.intent == "handoff":
                yield self._say(L["handoff"])
                yield {"event": "handoff", "data": self._handoff(route, lang)}
            elif route.intent == "listing_search":
                async for ev in self._listings(route, L, lang):
                    yield ev
            else:
                async for ev in self._rag(text, route, req, L, lang):
                    yield ev
        finally:
            metrics.LATENCY.observe(time.monotonic() - started)
        yield {"event": "done", "data": {"elapsed_ms": int((time.monotonic() - started) * 1000)}}

    @staticmethod
    def _say(text: str) -> dict:
        return {"event": "token", "data": {"text": text}}

    def _handoff(self, route: Route, lang: str) -> dict:
        brands = config.load("sources")["brands"]
        wanted = [b for b in route.brands if b in brands] or target_brands()
        return {
            "contacts": [{"brand": b, "name": brand_name(b, lang), "url": brands[b]["official_site"]} for b in wanted]
        }

    # ---- listings ---------------------------------------------------------------------------
    async def _listings(self, route: Route, L: dict, lang: str) -> AsyncIterator[dict]:
        brand = route.brands[0] if route.brands else self.router.cfg["listing_brand"]
        name = brand_name(brand, lang)
        f = route.filters
        yield {"event": "filters", "data": {k: v for k, v in asdict(f).items() if v not in (None, {}, "")}}
        if not self.kb.has_listings():
            yield self._say(L["listing_none_kb"].format(brand=name))
            yield {"event": "handoff", "data": self._handoff(route, lang)}
            return
        rows, total = self.kb.search_listings(f)
        if not rows:
            yield self._say(L["listing_none"].format(brand=name))
            return
        parts = [L["listing_intro"].format(total=total, brand=name, shown=len(rows))]
        stats = self.kb.listing_stats(f)
        if stats.get("min"):
            parts.append(
                L["listing_stats"].format(
                    min=_fmt_money(stats["min"]),
                    max=_fmt_money(stats["max"]),
                    median=_fmt_money(stats["median"]),
                    currency=stats["currency"],
                )
            )
        parts.append(L["listing_note"])
        yield self._say("\n\n".join(parts))
        yield {"event": "listings", "data": {"brand": brand, "brand_name": name, "items": rows}}
        yield {
            "event": "citations",
            "data": [
                {
                    "n": i + 1,
                    "title": r["title"],
                    "url": r["url"],
                    "fetched_at": r["fetched_at"],
                    "brand": brand,
                    "brand_name": name,
                    "source": "listing",
                }
                for i, r in enumerate(rows)
            ],
        }

    # ---- retrieval-augmented answers -------------------------------------------------------
    def _retrieve(self, text: str, route: Route, req: ChatRequest) -> list[Hit]:
        query = text
        prev = [t.content for t in req.history if t.role == "user"][-1:]
        if prev and len(text) < 60:  # short follow-ups ("and in Oman?") need the previous question
            query = f"{prev[0]} {text}"
        brands = [b for b in route.brands if b in config.load("sources")["brands"]]
        if len(brands) == 1:
            return self.kb.search(query, brand=brands[0])
        return self.kb.search(query)

    async def _rag(self, text: str, route: Route, req: ChatRequest, L: dict, lang: str) -> AsyncIterator[dict]:
        hits = self._retrieve(text, route, req)
        on_topic = (
            bool(route.brands)
            or any(w in text.lower() for w in self.router.cfg["domain_words"])
            or self.kb.mentions_known_entity(text)
        )
        confident = self.kb.is_confident(hits, on_topic)
        yield {
            "event": "retrieval",
            "data": [
                {
                    "title": h.title,
                    "brand": h.brand,
                    "score": round(h.score, 4),
                    "bm25_rank": h.bm25_rank,
                    "dense_rank": h.dense_rank,
                    "dense_sim": None if h.dense_sim is None else round(h.dense_sim, 3),
                }
                for h in hits
            ],
        }
        if not confident:
            metrics.EMPTY_RETRIEVAL.inc()
            reason = "unknown" if on_topic else "offtopic"
            metrics.REFUSALS.labels(reason=reason).inc()
            yield self._say(L["refuse_unknown"] if on_topic else L["refuse_offtopic"])
            if on_topic:
                yield {"event": "handoff", "data": self._handoff(route, lang)}
            return

        sources = self._format_sources(hits, lang)
        over = self.llm.check_budget(req.session_id) if self.llm.enabled else "disabled"
        meta: dict = {}
        answer = ""
        if over is None:
            try:
                async for delta in self.llm.stream(self._messages(text, sources, req, lang), meta):
                    answer += delta
                    yield {"event": "token", "data": {"text": delta}}
            except LLMUnavailable as exc:
                log.warning("chat.degraded", reason=str(exc)[:200])
                if answer:  # a partial stream: close it cleanly rather than append a second answer
                    yield self._say("…")
                answer = answer or ""
        if not answer:
            metrics.DEGRADED.inc()
            answer = self._extractive(hits, L)
            yield self._say(answer)
            meta["model"] = "extractive-fallback"

        missing = guard.unsupported_numbers(answer, sources, self.app["numeric_check"]["min_digits"])
        if missing and meta.get("model") != "extractive-fallback":
            metrics.UNSUPPORTED_NUMBERS.inc(len(missing))
            yield {
                "event": "notice",
                "data": {"kind": "unverified_numbers", "text": L["numbers_flag"].format(numbers=", ".join(missing))},
            }
        stale_days = self.rcfg["stale_after_days"]
        cited = self._cited(answer, hits)
        if any(_age_days(h.fetched_at) > stale_days for h in cited):
            yield {"event": "notice", "data": {"kind": "stale", "text": L["stale_note"].format(days=stale_days)}}
        yield {
            "event": "citations",
            "data": [
                {
                    "n": hits.index(h) + 1,
                    "title": h.title,
                    "url": h.url,
                    "fetched_at": h.fetched_at,
                    "brand": h.brand,
                    "brand_name": brand_name(h.brand, lang),
                    "source": h.source_name,
                }
                for h in cited
            ],
        }
        yield {"event": "model", "data": {"model": meta.get("model"), "verified": not missing}}

    def _format_sources(self, hits: list[Hit], lang: str) -> str:
        blocks = []
        for i, h in enumerate(hits, start=1):
            date = (h.fetched_at or "")[:10]
            blocks.append(
                f'<source n="{i}" company="{brand_name(h.brand, "en")}" title="{h.title}" '
                f'fetched="{date}" origin="{h.source_name}">\n{h.text}\n</source>'
            )
        return "\n".join(blocks)

    def _messages(self, text: str, sources: str, req: ChatRequest, lang: str) -> list[dict]:
        brands = config.load("sources")["brands"]
        targets = target_brands()
        parent = next((k for k, v in brands.items() if v.get("role") == "parent"), None)
        system = config.prompt("system").format(
            brands=" and ".join(brand_name(b, "en") for b in targets),
            parent=brand_name(parent, "en") if parent else "the parent company",
            language="Arabic" if lang == "ar" else "English",
            example_brand=brand_name(targets[0], "en"),
        )
        msgs = [{"role": "system", "content": system}]
        budget = self.app["history_chars"]
        for t in req.history[-self.app["history_turns"] :]:
            content = guard.redact(t.content)[:budget]
            if t.role in ("user", "assistant") and content:
                msgs.append({"role": t.role, "content": content})
        msgs.append(
            {
                "role": "user",
                "content": config.prompt("answer").format(
                    sources=sources,
                    question=guard.redact(text),
                    today=datetime.now(UTC).date().isoformat(),
                    stale_days=self.rcfg["stale_after_days"],
                ),
            }
        )
        return msgs

    @staticmethod
    def _cited(answer: str, hits: list[Hit]) -> list[Hit]:
        nums = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
        cited = [h for i, h in enumerate(hits, start=1) if i in nums]
        return cited or hits[:3]

    @staticmethod
    def _extractive(hits: list[Hit], L: dict) -> str:
        lines = [L["degraded"]]
        for i, h in enumerate(hits[:3], start=1):
            snippet = re.sub(r"\s+", " ", h.text)[:320].rsplit(" ", 1)[0]
            lines.append(f"- **{h.title}**: {snippet}… [{i}]")
        return "\n".join(lines)
