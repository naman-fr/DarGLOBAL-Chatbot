"""JSON logs with request ids, and Prometheus metrics."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

from prometheus_client import Counter, Histogram

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class _Log:
    def __init__(self) -> None:
        self._logger = logging.getLogger("concierge")
        if not self._logger.handlers:
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(h)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False

    def _emit(self, level: int, event: str, **fields) -> None:
        rec = {
            "ts": round(time.time(), 3),
            "level": logging.getLevelName(level).lower(),
            "event": event,
            "request_id": request_id.get(),
            **fields,
        }
        self._logger.log(level, json.dumps(rec, ensure_ascii=False, default=str))

    def info(self, event: str, **fields) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields) -> None:
        self._emit(logging.ERROR, event, **fields)


log = _Log()


class metrics:  # namespace
    REQUESTS = Counter("concierge_chat_requests_total", "Chat requests", ["intent", "lang"])
    LATENCY = Histogram(
        "concierge_chat_latency_seconds", "End-to-end chat latency", buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32)
    )
    LLM_LATENCY = Histogram(
        "concierge_llm_latency_seconds", "LLM stream latency", ["model"], buckets=(0.5, 1, 2, 4, 8, 16, 32)
    )
    LLM_FALLBACKS = Counter("concierge_llm_fallbacks_total", "Replies served after a fallback")
    LLM_BREAKER_OPEN = Counter("concierge_llm_breaker_open_total", "Model breaker trips", ["model"])
    DEGRADED = Counter("concierge_degraded_answers_total", "Extractive answers because no model answered")
    EMPTY_RETRIEVAL = Counter("concierge_empty_retrieval_total", "Questions with no confident KB hit")
    REFUSALS = Counter("concierge_refusals_total", "Refusals", ["reason"])
    UNSUPPORTED_NUMBERS = Counter("concierge_unsupported_numbers_total", "Answer numbers not found in context")
    LEADS = Counter("concierge_leads_total", "Leads captured")
