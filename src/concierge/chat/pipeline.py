"""The chat flow as an explicit graph of small steps.

    sanitize -> normalise -> input rails -> route --+-- blocked / injection / greeting / handoff -> fixed reply
                                                    +-- listing_search -> SQL filters -> cards (no LLM)
                                                    +-- rag -> graph seeds -> retrieve (BM25 + dense + graph, RRF)
                                                               -> confident? -- no -> refuse + handoff
                                                                    | yes
                                                                    v
                                     generate (LLM, streamed, with sources + graph relations)
                                     (LLM down -> extractive answer with citations)
                                                                    v
                                     output rails (citations, grounding, numbers, leakage, PII, blending, advice)
                                                                    v
                                     citations -> follow-up suggestions from the graph -> trace

Every step emits events; the API turns them into SSE.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import numpy as np

from concierge import config
from concierge.chat import guard, guardrails
from concierge.chat.llm import LLMUnavailable, OpenRouter
from concierge.chat.router import Route, Router
from concierge.kb.graph import GraphContext
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
        trace = Trace()
        text = guardrails.normalise(guard.sanitize(req.message, self.app["max_input_chars"]))
        verdict = guardrails.check_input(text)
        route = self.router.route(text)
        if verdict.block:
            route.intent = "blocked"
        lang = route.lang if text else (req.lang or "en")
        L = config.locale(lang)
        trace.step("route", intent=route.intent, lang=lang, brands=route.brands, input_rail=verdict.block or "pass")
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
            if route.intent == "blocked":
                metrics.REFUSALS.labels(reason=verdict.block).inc()
                yield self._say(L["refuse_unsafe"] if verdict.block == "unsafe" else L["refuse_injection"])
            elif route.intent == "injection":
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
                async for ev in self._rag(text, route, req, L, lang, trace, verdict.advice):
                    yield ev
        finally:
            metrics.LATENCY.observe(time.monotonic() - started)
        yield {"event": "trace", "data": trace.steps}
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
    def _retrieve(self, text: str, route: Route, req: ChatRequest) -> tuple[list[Hit], GraphContext]:
        query = text
        prev = [t.content for t in req.history if t.role == "user"][-1:]
        if prev and len(text) < 60:  # short follow-ups ("and in Oman?") need the previous question
            query = f"{prev[0]} {text}"
        graph = self.kb.graph_context(text)
        if not graph.seeds and query != text:
            graph = self.kb.graph_context(query)
        brands = [b for b in route.brands if b in config.load("sources")["brands"]]
        if len(brands) == 1:
            return self.kb.search(query, brand=brands[0], graph=graph), graph
        return self.kb.search(query, graph=graph), graph

    def _graph_event(self, graph: GraphContext, hits: list[Hit], lang: str) -> dict:
        g = self.kb.graph
        facts = g.facts(graph, [h.rowid for h in hits], lang)
        return {
            "event": "graph",
            "data": {
                "seeds": [{"id": e["id"], "name": g.name(e["id"], lang), "type": e["type"]} for e in graph.seeds],
                "neighbours": [
                    {"id": e["id"], "name": g.name(e["id"], lang), "type": e["type"]} for e in graph.neighbours[:8]
                ],
                "wanted_type": graph.wanted_type,
                "facts": [
                    {
                        "a": f.a_name,
                        "a_type": f.a_type,
                        "label": f.label,
                        "b": f.b_name,
                        "b_type": f.b_type,
                        "cites": f.cites,
                    }
                    for f in facts
                ],
            },
        }

    def _followups(self, graph: GraphContext, hits: list[Hit], L: dict, lang: str) -> list[str]:
        """Next questions from the graph: neighbours of what was asked, or the entities the cited
        passages are about when the question named none."""
        g = self.kb.graph
        templates = L.get("followup_templates", {})
        asked = {e["id"] for e in graph.seeds}
        pool = [e["id"] for e in graph.neighbours]
        if not pool:
            counts: dict[str, int] = {}
            for h in hits[:4]:
                for eid in g.chunk_entities.get(h.rowid, ()):
                    counts[eid] = counts.get(eid, 0) + 1
            pool = sorted(counts, key=lambda e: -counts[e])
        out: list[str] = []
        for eid in pool:
            e = g.entities.get(eid)
            if not e or eid in asked or e["type"] not in templates:
                continue
            q = templates[e["type"]].format(name=g.name(eid, lang))
            if q not in out:
                out.append(q)
            if len(out) == 3:
                break
        return out

    async def _rag(
        self, text: str, route: Route, req: ChatRequest, L: dict, lang: str, trace: Trace, advice: bool = False
    ) -> AsyncIterator[dict]:
        t0 = time.monotonic()
        hits, graph = self._retrieve(text, route, req)
        on_topic = (
            bool(route.brands)
            or any(w in text.lower() for w in self.router.cfg["domain_words"])
            or self.kb.mentions_known_entity(text)
            or bool(graph.seeds)
        )
        confident = self.kb.is_confident(hits, on_topic)
        trace.step(
            "retrieve",
            ms=int((time.monotonic() - t0) * 1000),
            hits=len(hits),
            from_bm25=sum(h.bm25_rank is not None for h in hits),
            from_dense=sum(h.dense_rank is not None for h in hits),
            from_graph=sum(h.graph_rank is not None for h in hits),
            graph_entities=[e["name"] for e in graph.seeds],
            best_similarity=round(max((h.dense_sim or 0.0) for h in hits[:3]), 3) if hits else None,
            confident=confident,
        )
        yield {
            "event": "retrieval",
            "data": [
                {
                    "title": h.title,
                    "brand": h.brand,
                    "score": round(h.score, 4),
                    "bm25_rank": h.bm25_rank,
                    "dense_rank": h.dense_rank,
                    "graph_rank": h.graph_rank,
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

        if graph.seeds:
            yield self._graph_event(graph, hits, lang)
        sources = self._format_sources(hits, lang)
        relations = "\n".join(
            f"{f.a_name} ({f.a_type}) — {f.label} — {f.b_name} ({f.b_type}) " + "".join(f"[{c}]" for c in f.cites)
            for f in self.kb.graph.facts(graph, [h.rowid for h in hits], "en")
        )
        over = self.llm.check_budget(req.session_id) if self.llm.enabled else "disabled"
        meta: dict = {}
        answer = ""
        messages = self._messages(text, sources, req, lang, relations or "(none)")
        t1 = time.monotonic()
        if over is None:
            try:
                async for delta in self.llm.stream(messages, meta):
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

        trace.step("generate", ms=int((time.monotonic() - t1) * 1000), model=meta.get("model"), budget=over or "ok")

        # ---- output rails ----
        t2 = time.monotonic()
        report = guardrails.check_output(
            answer,
            hits,
            sources,
            messages[0]["content"],
            embed=self._embed_fn(),
            hit_vectors=self._hit_vectors(hits),
            min_digits=self.app["numeric_check"]["min_digits"],
        )
        if report.replaced:
            metrics.REFUSALS.labels(reason="leakage").inc()
            answer = L["refuse_injection"]
            yield {"event": "replace", "data": {"text": answer}}
        elif report.answer != answer:
            answer = report.answer
            yield {"event": "replace", "data": {"text": answer}}
        missing = report.unsupported_numbers
        if missing and meta.get("model") != "extractive-fallback":
            metrics.UNSUPPORTED_NUMBERS.inc(len(missing))
            yield {
                "event": "notice",
                "data": {"kind": "unverified_numbers", "text": L["numbers_flag"].format(numbers=", ".join(missing))},
            }
        if advice or report.advice:
            yield {"event": "notice", "data": {"kind": "advice", "text": L["advice_note"]}}
        yield {"event": "guardrails", "data": report.to_event()}
        trace.step("guardrails", ms=int((time.monotonic() - t2) * 1000), status=report.status)
        if report.replaced:
            yield {"event": "model", "data": {"model": meta.get("model"), "verified": False}}
            return
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
        followups = self._followups(graph, cited, L, lang)
        if followups:
            yield {"event": "followups", "data": followups}
        yield {"event": "model", "data": {"model": meta.get("model"), "verified": not missing}}

    def _embed_fn(self):
        model = self.kb.embedder()
        return None if model is None else model.embed

    def _hit_vectors(self, hits: list[Hit]) -> np.ndarray | None:
        if self.kb.vectors is None or not hits:
            return None
        return np.asarray(self.kb.vectors[[self.kb.row_index(h.rowid) for h in hits]], dtype=np.float32)

    def _format_sources(self, hits: list[Hit], lang: str) -> str:
        blocks = []
        for i, h in enumerate(hits, start=1):
            date = (h.fetched_at or "")[:10]
            blocks.append(
                f'<source n="{i}" company="{brand_name(h.brand, "en")}" title="{h.title}" '
                f'fetched="{date}" origin="{h.source_name}">\n{h.text}\n</source>'
            )
        return "\n".join(blocks)

    def _messages(self, text: str, sources: str, req: ChatRequest, lang: str, relations: str = "(none)") -> list[dict]:
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
                    relations=relations,
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


class Trace:
    """Per-request step log, shown in the UI's "How this answer was built" panel."""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def step(self, name: str, **detail) -> None:
        self.steps.append({"step": name, **{k: v for k, v in detail.items() if v not in (None, [], "")}})
