# syntax=docker/dockerfile:1.7
# Stage 1 (kb): prepare the int8 embedding model and rebuild the KB vectors with that exact model, from
#               the committed records snapshot. Query and document vectors therefore always match.
# Stage 2 (deps): runtime wheels only (no fastembed, no onnx, no crawler libraries).
# Stage 3 (runtime): slim, non-root, KB and model baked in, so a cold start never crawls.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS kb
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/build/src \
    CONCIERGE_ROOT=/build
WORKDIR /build
COPY requirements.in requirements-ingest.in requirements-ingest.lock ./
RUN pip install --no-deps -r requirements-ingest.lock
COPY config/retrieval.yaml ./config/
# Only the modules the KB build imports, so edits elsewhere in src/ keep this stage cached.
COPY src/concierge/__init__.py src/concierge/config.py ./src/concierge/
COPY src/concierge/kb ./src/concierge/kb
COPY data/processed/records.jsonl data/processed/crawl_report.json ./data/processed/
RUN python -m concierge.kb.build > /build/kb-build.json && rm -rf data/models/cache

FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-deps -r requirements.lock

FROM ${PYTHON_IMAGE} AS runtime
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="property-concierge" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src CONCIERGE_ROOT=/app \
    OMP_NUM_THREADS=1 MALLOC_ARENA_MAX=2 \
    GIT_SHA=${GIT_SHA} PORT=8000
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY --from=kb --chown=app:app /build/data/models/runtime ./data/models/runtime
COPY --from=kb --chown=app:app /build/data/kb ./data/kb
COPY --chown=app:app config ./config
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app locales ./locales
COPY --chown=app:app web ./web
COPY --chown=app:app src ./src
COPY --chown=app:app docs/evals.json ./docs/evals.json
RUN mkdir -p /app/data/runtime && chown app:app /app/data/runtime
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz',timeout=4)"
# One worker: the free tier has 512 MB and a fraction of a CPU; the app is I/O bound on the LLM.
CMD ["sh", "-c", "exec uvicorn concierge.api.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
