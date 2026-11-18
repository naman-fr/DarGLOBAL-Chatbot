"""HTTP surface: SPA, SSE chat, suggestions, leads, feedback, health, readiness, metrics, admin."""

from __future__ import annotations

import asyncio
import hmac
import json
import random
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from concierge import config
from concierge.api.runtime_store import CRMAdapter, RuntimeStore
from concierge.chat.llm import OpenRouter
from concierge.chat.pipeline import ChatRequest, Concierge, Turn, brand_name, target_brands
from concierge.kb.store import KB
from concierge.observability import log, metrics, request_id

APP_CFG = config.load("app")
WEB_DIR = config.path("web")


class State:
    kb: KB | None = None
    concierge: Concierge | None = None
    llm: OpenRouter | None = None
    store: RuntimeStore | None = None
    crm: CRMAdapter | None = None
    started: float = time.time()
    load_error: str | None = None


S = State()
_background: set[asyncio.Task] = set()  # keep references so tasks are not garbage-collected mid-run


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _load_kb() -> None:
    if config.env("CONCIERGE_SKIP_KB_LOAD"):  # tests inject their own KB
        return
    try:
        S.kb = KB()
        S.kb.embed_query("warm up")  # load the ONNX model now, not on the first visitor's question
        S.concierge = Concierge(S.kb, S.llm)
        log.info("kb.loaded", kb_version=S.kb.version, chunks=S.kb.manifest["chunks"])
    except Exception as exc:  # keep serving /healthz so the platform can show the failure
        S.load_error = repr(exc)
        log.error("kb.load_failed", error=S.load_error)


@asynccontextmanager
async def lifespan(_: FastAPI):
    S.llm = OpenRouter()
    S.store = RuntimeStore()
    S.crm = CRMAdapter()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _load_kb)
    if S.llm.enabled:
        spawn(S.llm.discover())
    yield


app = FastAPI(title="concierge", lifespan=lifespan, docs_url=None, redoc_url=None)
if APP_CFG["cors_allow_origins"]:
    app.add_middleware(CORSMiddleware, allow_origins=APP_CFG["cors_allow_origins"], allow_methods=["GET", "POST"])

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
}


@app.middleware("http")
async def envelope(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request_id.set(rid)
    if int(request.headers.get("content-length") or 0) > 16_384:
        return JSONResponse({"error": "too_large"}, status_code=413)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:  # never leak a stack trace to the browser
        log.error("http.unhandled", path=request.url.path, error=repr(exc)[:300])
        response = JSONResponse({"error": "internal"}, status_code=500)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    response.headers["X-Request-ID"] = rid
    if not request.url.path.startswith(("/healthz", "/readyz", "/metrics", "/assets")):
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=int((time.monotonic() - started) * 1000),
        )
    return response


# ---- rate limiting (in-process; one instance on the free tier) ------------------------------
_hits: dict[str, deque] = defaultdict(deque)


def _limited(key: str, per_minute: int) -> bool:
    q, now = _hits[key], time.time()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= per_minute:
        return True
    q.append(now)
    return False


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


# ---- health -------------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "uptime_s": int(time.time() - S.started)}


@app.get("/readyz")
async def readyz():
    kb_ok = S.kb is not None and S.concierge is not None
    llm = S.llm
    body = {
        "ready": kb_ok,
        "kb_version": S.kb.version if S.kb else None,
        "kb_error": S.load_error,
        "llm": {
            "configured": bool(llm and llm.enabled),
            "models_known": len(llm.state.models) if llm else 0,
            "last_ok_model": llm.state.last_ok_model if llm else None,
            "mode": "llm" if llm and llm.enabled else "degraded-extractive",
        },
    }
    return JSONResponse(body, status_code=200 if kb_ok else 503)


@app.get("/metrics")
async def prom():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---- config and suggestions -----------------------------------------------------------------
@app.get("/api/config")
async def ui_config():
    brand_cfg = config.load("brand")
    sources = config.load("sources")["brands"]
    return {
        "theme": brand_cfg["theme"],
        "wordmark": brand_cfg["wordmark"],
        "brands": [
            {
                "key": k,
                "name": v["name"],
                "name_ar": v.get("name_ar"),
                "url": v["official_site"],
                "role": v.get("role", "brand"),
            }
            for k, v in sources.items()
        ],
        "kb_version": S.kb.version if S.kb else None,
        "langs": ["en", "ar"],
    }


@app.get("/api/locale/{lang}")
async def ui_locale(lang: str):
    if lang not in ("en", "ar"):
        raise HTTPException(404)
    return config.locale(lang)


@app.get("/api/suggestions")
async def suggestions(lang: str = "en"):
    if S.kb is None:
        return {"items": []}
    L = config.locale(lang if lang in ("en", "ar") else "en")
    cfg = APP_CFG["suggestions"]
    rows = S.kb.suggestion_rows(lang)
    pools = {}
    for brand in target_brands():
        mine = [r for r in rows if r["brand"] == brand]
        random.shuffle(mine)
        pools[brand] = mine
    out: list[dict] = []
    while len(out) < cfg["count"] and any(pools.values()):  # round-robin so one brand cannot crowd out the other
        for brand, pool in pools.items():
            if pool and len(out) < cfg["count"]:
                r = pool.pop()
                name = brand_name(brand, lang)
                tpl = L["suggest_templates"].get(
                    r.get("section") or "", L["suggest_templates"].get(r["kind"], "{title}")
                )
                out.append({"brand": brand, "brand_name": name, "text": tpl.format(brand_name=name, **r)})
    return {"items": out}


# ---- chat ------------------------------------------------------------------------------------
class TurnIn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=6, max_length=64)
    history: list[TurnIn] = Field(default_factory=list, max_length=20)
    lang: str | None = None


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    rl = APP_CFG["rate_limit"]
    if _limited(f"ip:{client_ip(request)}", rl["per_ip_per_minute"]) or _limited(
        f"s:{body.session_id}", rl["per_session_per_minute"]
    ):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    if S.concierge is None:
        return JSONResponse({"error": "warming_up"}, status_code=503)
    req = ChatRequest(
        message=body.message,
        session_id=body.session_id,
        lang=body.lang,
        history=[Turn(t.role, t.content) for t in body.history],
    )
    rid = request_id.get()

    async def events():
        request_id.set(rid)
        intent, lang, refused = "", "en", False
        try:
            async for ev in S.concierge.run(req):
                if ev["event"] == "meta":
                    intent, lang = ev["data"]["intent"], ev["data"]["lang"]
                if ev["event"] == "handoff" and intent == "rag":
                    refused = True
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False, default=str)}\n\n"
        except Exception as exc:
            log.error("chat.failed", error=repr(exc)[:300])
            L = config.locale(lang)
            yield f"event: error\ndata: {json.dumps({'text': L['error_generic']}, ensure_ascii=False)}\n\n"
        if refused and S.store:
            S.store.add_unanswered(body.message, lang)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ---- leads and feedback ------------------------------------------------------------------------
class LeadIn(BaseModel):
    name: str = Field(max_length=80)
    contact: str = Field(max_length=120)
    role: str = Field(default="", max_length=20)
    budget: str = Field(default="", max_length=40)
    timeline: str = Field(default="", max_length=40)
    country: str = Field(default="", max_length=60)
    interest: str = Field(default="", max_length=200)
    brand: str = Field(default="", max_length=20)
    lang: str = Field(default="en", max_length=2)
    consent: bool = False
    summary: str = Field(default="", max_length=1000)


@app.post("/api/lead")
async def lead(body: LeadIn, request: Request):
    if _limited(f"lead:{client_ip(request)}", 3):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    data = body.model_dump()
    bad = S.store.validate_lead(data)
    if bad:
        return JSONResponse({"error": "invalid", "field": bad}, status_code=422)
    if data["brand"] and data["brand"] not in config.load("sources")["brands"]:
        data["brand"] = ""
    lead_id = S.store.add_lead(data)
    metrics.LEADS.inc()
    spawn(S.store.flush_outbox(S.crm))
    return {"ok": True, "lead_id": lead_id}


class FeedbackIn(BaseModel):
    rating: int
    reason: str = Field(default="", max_length=300)
    question: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=100)


@app.post("/api/feedback")
async def feedback(body: FeedbackIn, request: Request):
    if _limited(f"fb:{client_ip(request)}", 20):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    S.store.add_feedback({**body.model_dump(), "kb_version": S.kb.version if S.kb else ""})
    return {"ok": True}


# ---- admin ---------------------------------------------------------------------------------------
@app.get("/api/admin/status")
async def admin_status(request: Request):
    token = config.env(APP_CFG["admin_token_env"])
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(given, token):
        raise HTTPException(401)
    llm = S.llm
    return {
        "kb": S.kb.manifest if S.kb else None,
        "crawl_report": _crawl_report(),
        "llm": {
            "models": llm.state.models[:10],
            "last_ok_model": llm.state.last_ok_model,
            "last_error": llm.state.last_error,
            "requests_today": llm.state.day_count,
            "benched": {
                m: round(b.open_until - time.time())
                for m, b in llm.state.breakers.items()
                if b.open_until > time.time()
            },
        },
        "evals": _json_or_none(config.path("docs", "evals.json")),
        "runtime": S.store.summary(),
    }


def _json_or_none(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _crawl_report():
    return _json_or_none(config.path("data", "kb", "crawl_report.json"))


# ---- SPA -------------------------------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})
