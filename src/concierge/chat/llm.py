"""OpenRouter client for free models: discovery, fallback chain, retries, breakers, budgets."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from concierge import config
from concierge.observability import log, metrics


class LLMUnavailable(Exception):
    pass


@dataclass
class Breaker:
    failures: int = 0
    open_until: float = 0.0


@dataclass
class LLMState:
    models: list[str] = field(default_factory=list)
    discovered_at: float = 0.0
    breakers: dict[str, Breaker] = field(default_factory=dict)
    day: str = ""
    day_count: int = 0
    session_hits: dict[str, list[float]] = field(default_factory=dict)
    last_ok_model: str | None = None
    last_error: str | None = None


class OpenRouter:
    def __init__(self) -> None:
        self.cfg = config.load("llm")
        self.key = config.env("OPENROUTER_API_KEY")
        self.state = LLMState()
        self._lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.cfg["timeout_seconds"], connect=10))

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.key}",
            "HTTP-Referer": config.env("APP_URL", self.cfg["app_referer"]),
            "X-Title": self.cfg["app_title"],
        }

    # ---- discovery -----------------------------------------------------------------------
    async def discover(self, force: bool = False) -> list[str]:
        fresh = time.time() - self.state.discovered_at < self.cfg["discovery_refresh_hours"] * 3600
        if self.state.models and fresh and not force:
            return self.state.models
        async with self._lock:
            try:
                r = await self.client.get(f"{self.cfg['base_url']}/models", timeout=20)
                r.raise_for_status()
                models = rank_free_models(r.json().get("data", []), self.cfg)
                if models:
                    self.state.models = models
                    self.state.discovered_at = time.time()
                    log.info("llm.discovered", count=len(models), top=models[:4])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("llm.discovery_failed", error=str(exc))
            if not self.state.models:
                self.state.models = list(self.cfg["static_fallback"])
        return self.state.models

    def candidates(self) -> list[str]:
        now = time.time()
        usable = [m for m in self.state.models if self.state.breakers.get(m, Breaker()).open_until <= now]
        return [*usable, self.cfg["last_resort"]]

    # ---- budgets ---------------------------------------------------------------------------
    def check_budget(self, session_id: str) -> str | None:
        today = time.strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day, self.state.day_count = today, 0
        if self.state.day_count >= self.cfg["budget"]["global_requests_per_day"]:
            return "global"
        hits = [t for t in self.state.session_hits.get(session_id, []) if t > time.time() - 3600]
        if len(hits) >= self.cfg["budget"]["session_requests_per_hour"]:
            return "session"
        hits.append(time.time())
        self.state.session_hits[session_id] = hits
        self.state.day_count += 1
        return None

    def _fail(self, models: list[str], error: str) -> None:
        self.state.last_error = error
        for m in models:
            b = self.state.breakers.setdefault(m, Breaker())
            b.failures += 1
            if b.failures >= self.cfg["breaker_failures"]:
                b.open_until = time.time() + self.cfg["breaker_cooldown_seconds"]
                b.failures = 0
                metrics.LLM_BREAKER_OPEN.labels(model=m).inc()

    def _ok(self, model: str) -> None:
        self.state.breakers.pop(model, None)
        self.state.last_ok_model = model
        self.state.last_error = None

    # ---- chat ----------------------------------------------------------------------------
    async def stream(self, messages: list[dict], meta: dict) -> AsyncIterator[str]:
        """Yield text deltas. Sets meta['model'] to the model that actually served the reply."""
        if not self.enabled:
            raise LLMUnavailable("no OPENROUTER_API_KEY configured")
        await self.discover()
        cands = self.candidates()
        per = self.cfg["models_per_request"]
        groups = [cands[i : i + per] for i in range(0, len(cands), per)][: self.cfg["max_attempts"]]
        last = ""
        for attempt, group in enumerate(groups):
            body = {
                "models": group,
                "messages": messages,
                "stream": True,
                "temperature": self.cfg["temperature"],
                "max_tokens": self.cfg["max_tokens"],
            }
            started = time.monotonic()
            got_text = False
            try:
                async with self.client.stream(
                    "POST", f"{self.cfg['base_url']}/chat/completions", headers=self.headers(), json=body
                ) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode("utf-8", "replace")[:300]
                        raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {detail}", request=r.request, response=r)
                    async for delta, model in _sse_deltas(r, self.cfg["first_token_timeout_seconds"]):
                        if model:
                            meta["model"] = model
                        if delta:
                            got_text = True
                            yield delta
                if not got_text:
                    raise LLMUnavailable("empty completion")
                self._ok(meta.get("model", group[0]))
                metrics.LLM_LATENCY.labels(model=meta.get("model", "?")).observe(time.monotonic() - started)
                if attempt:
                    metrics.LLM_FALLBACKS.inc()
                return
            except (TimeoutError, httpx.HTTPError, LLMUnavailable) as exc:
                if got_text:  # partial answer already streamed; do not splice another model's text into it
                    raise LLMUnavailable(f"stream broke mid-answer: {exc}") from exc
                last = str(exc)
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                log.warning("llm.attempt_failed", group=group, status=status, error=last[:200])
                if status in (401, 402, 403):  # account problem, not a model problem: no retries, no benching
                    self.state.last_error = last
                    raise LLMUnavailable(f"OpenRouter rejected the account (HTTP {status})") from exc
                self._fail([m for m in group if m != self.cfg["last_resort"]], last)
                await asyncio.sleep(_backoff(attempt, status))
        raise LLMUnavailable(last or "all models failed")

    async def complete_json(self, messages: list[dict], schema: dict) -> dict | None:
        """Single non-streamed call with JSON-schema output; one repair retry, else None."""
        if not self.enabled:
            return None
        await self.discover()
        models = self.candidates()[: self.cfg["models_per_request"]]
        body = {
            "models": models,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 300,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "filters", "strict": True, "schema": schema},
            },
        }
        for _ in range(2):
            try:
                r = await self.client.post(
                    f"{self.cfg['base_url']}/chat/completions", headers=self.headers(), json=body
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"] or ""
                m = re.search(r"\{.*\}", content, re.S)
                return json.loads(m.group(0)) if m else None
            except (httpx.HTTPError, KeyError, ValueError):
                body["messages"] = [*messages, {"role": "user", "content": "Reply with valid JSON only."}]
        return None


def rank_free_models(data: list[dict], cfg: dict) -> list[str]:
    deny = [re.compile(p, re.I) for p in cfg["deny"]]
    prefer = [re.compile(p, re.I) for p in cfg["prefer"]]
    out = []
    for m in data:
        mid = m.get("id", "")
        pricing = m.get("pricing") or {}
        if str(pricing.get("prompt")) != "0" or str(pricing.get("completion")) != "0":
            continue
        if mid == cfg["last_resort"] or any(d.search(mid) for d in deny):
            continue
        if (m.get("context_length") or 0) < cfg["min_context"]:
            continue
        outputs = ((m.get("architecture") or {}).get("output_modalities")) or ["text"]
        if cfg["require_text_output"] and "text" not in outputs:
            continue
        rank = next((i for i, p in enumerate(prefer) if p.search(mid)), len(prefer))
        out.append((rank, -(m.get("context_length") or 0), mid))
    return [mid for _, _, mid in sorted(out)]


async def _sse_deltas(r: httpx.Response, first_token_timeout: float) -> AsyncIterator[tuple[str, str | None]]:
    lines = r.aiter_lines()
    first = True
    while True:
        try:
            line = await (asyncio.wait_for(lines.__anext__(), first_token_timeout) if first else lines.__anext__())
        except StopAsyncIteration:
            return
        if not line.startswith("data:"):
            continue  # OpenRouter sends ": OPENROUTER PROCESSING" keep-alives
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if "error" in chunk:
            raise LLMUnavailable(str(chunk["error"])[:300])
        choice = (chunk.get("choices") or [{}])[0]
        delta = (choice.get("delta") or {}).get("content") or ""
        if delta:
            first = False
        yield delta, chunk.get("model")


def _backoff(attempt: int, status: int) -> float:
    base = 1.5 if status == 429 else 0.5
    return base * (2**attempt) + random.uniform(0, 0.4)
