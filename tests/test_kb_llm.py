import asyncio
import json

import httpx
import pytest

from concierge.chat import llm as llm_mod
from concierge.kb.chunk import chunk_record
from concierge.kb.store import ListingFilters


def test_chunking_is_structure_aware(fixture_records):
    faq = next(r for r in fixture_records if r["kind"] == "faq")
    chunks = chunk_record(faq, 900, 150)
    assert len(chunks) == 1 and chunks[0]["text"].startswith("Q: ")
    long = {**fixture_records[0], "text": "\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))}
    chunks = chunk_record(long, 900, 150)
    assert len(chunks) > 3 and all(len(c["text"]) <= 900 for c in chunks)
    assert all(c["brand"] == "darglobal" and c["url"] for c in chunks)


def test_lexical_search_and_brand_filter(lexical_kb):
    hits = lexical_kb.search("London Stock Exchange listing", brand="darglobal")
    assert hits and all(h.brand == "darglobal" for h in hits)
    assert "London Stock Exchange" in hits[0].text


def test_listing_filters(lexical_kb):
    rows, total = lexical_kb.search_listings(ListingFilters(purpose="sale", city="Riyadh", max_price=1_000_000))
    assert total == 1 and rows[0]["listing_id"] == 101
    rows, total = lexical_kb.search_listings(ListingFilters(property_type="villa", purpose="rent"))
    assert total == 1 and rows[0]["city"] == "Jeddah"
    stats = lexical_kb.listing_stats(ListingFilters(city="Riyadh"))
    assert stats["min"] == 850000 and stats["max"] == 1450000


def test_kb_manifest_has_version(lexical_kb):
    assert lexical_kb.version and lexical_kb.manifest["records"] == 8


MODELS = {
    "data": [
        {"id": "google/gemma-4-31b-it:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 262144},
        {"id": "acme/big:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 1000000},
        {"id": "acme/paid", "pricing": {"prompt": "0.000001", "completion": "0.000002"}, "context_length": 1000000},
        {"id": "acme/tiny-1.7b:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 262144},
        {"id": "acme/music-lyria:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 262144},
        {"id": "acme/short:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 4096},
        {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 200000},
    ]
}


def test_free_model_ranking():
    from concierge import config

    ranked = llm_mod.rank_free_models(MODELS["data"], config.load("llm"))
    assert ranked == ["google/gemma-4-31b-it:free", "acme/big:free"]


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sse(model, *parts):
    lines = [": OPENROUTER PROCESSING"]
    for p in parts:
        lines.append("data: " + json.dumps({"model": model, "choices": [{"delta": {"content": p}}]}))
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def test_stream_falls_back_to_next_group(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        body = json.loads(request.content)
        calls.append(body["models"])
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, text=_sse(body["models"][0], "Hello ", "world [1]"))

    client = llm_mod.OpenRouter()
    client.cfg = {**client.cfg, "models_per_request": 1}
    client.client = _client(handler)
    monkeypatch.setattr(llm_mod, "_backoff", lambda *a: 0)
    meta = {}

    async def run():
        return "".join([d async for d in client.stream([{"role": "user", "content": "hi"}], meta)])

    assert asyncio.run(run()) == "Hello world [1]"
    assert calls[0] == ["google/gemma-4-31b-it:free"] and calls[1] == ["acme/big:free"]
    assert meta["model"] == "acme/big:free"


def test_stream_raises_when_everything_fails(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        return httpx.Response(503, text="down")

    client = llm_mod.OpenRouter()
    client.client = _client(handler)
    monkeypatch.setattr(llm_mod, "_backoff", lambda *a: 0)

    async def run():
        return [d async for d in client.stream([{"role": "user", "content": "hi"}], {})]

    with pytest.raises(llm_mod.LLMUnavailable):
        asyncio.run(run())
