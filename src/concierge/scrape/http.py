"""Polite HTTP client: robots.txt, per-host pacing, conditional requests, retries, circuit breaker.

It never tries to get past a bot wall. A challenge page is reported as `blocked` and the host's
breaker counts it like a 403.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from protego import Protego

from concierge import config


@dataclass
class FetchResult:
    url: str
    status: int
    text: str = ""
    fetched_at: str = ""
    from_cache: bool = False
    outcome: str = "ok"  # ok | not_modified | robots_disallowed | blocked | error | breaker_open
    content_hash: str = ""
    error: str = ""


class TransportError(Exception):
    pass


@dataclass
class Response:
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class CurlTransport:
    """Plain curl as the transport.

    Some CDNs fingerprint Python TLS stacks and challenge them regardless of robots.txt. curl with the
    same honest User-Agent is served normally. This is not impersonation: the UA still names the bot.
    """

    def __init__(self, user_agent: str, timeout: float, extra_headers: dict[str, str]) -> None:
        self.curl = shutil.which("curl") or "curl"
        self.user_agent = user_agent
        self.timeout = timeout
        self.extra = extra_headers

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        hdrs = {**self.extra, **(headers or {})}
        with tempfile.TemporaryDirectory() as tmp:
            head_file, body_file = f"{tmp}/h", f"{tmp}/b"
            cmd = [
                self.curl,
                "-sS",
                "-L",
                "--compressed",
                "--max-redirs",
                "5",
                "-m",
                str(int(self.timeout)),
                "-A",
                self.user_agent,
                "-D",
                head_file,
                "-o",
                body_file,
                "-w",
                "%{http_code}",
            ]
            for k, v in hdrs.items():
                cmd += ["-H", f"{k}: {v}"]
            cmd.append(url)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 10)
            if proc.returncode != 0:
                raise TransportError(proc.stderr.strip() or f"curl exit {proc.returncode}")
            status = int(proc.stdout.strip() or 0)
            parsed: dict[str, str] = {}
            with open(head_file, encoding="latin-1") as fh:
                for line in fh:  # with -L the last header block wins
                    if line.upper().startswith("HTTP/"):
                        parsed = {}
                    elif ":" in line:
                        k, v = line.split(":", 1)
                        parsed[k.strip().lower()] = v.strip()
            with open(body_file, "rb") as fh:
                body = fh.read()
        return Response(status, parsed, body)


@dataclass
class HostState:
    robots: Protego | None = None
    robots_loaded: bool = False
    last_request: float = 0.0
    strikes: int = 0
    stats: dict[str, int] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class PoliteClient:
    def __init__(self, cfg: dict | None = None, contact: str | None = None) -> None:
        self.cfg = cfg or config.load("crawler")
        ua = self.cfg["user_agent"]
        contact = contact or config.env("CRAWLER_CONTACT")
        if contact:
            ua = ua.replace("contact via CRAWLER_CONTACT", f"contact {contact}")
        self.user_agent = ua
        self.client = CurlTransport(ua, self.cfg["timeout_seconds"], {"Accept-Language": "en,ar;q=0.8"})
        self.hosts: dict[str, HostState] = {}
        self.cache_dir = config.path(self.cfg["cache_dir"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log: list[dict] = []

    # ---- robots -------------------------------------------------------------------------
    def _host(self, url: str) -> tuple[str, HostState]:
        host = urlparse(url).netloc
        return host, self.hosts.setdefault(host, HostState())

    def _load_robots(self, url: str, state: HostState) -> None:
        if state.robots_loaded:
            return
        state.robots_loaded = True
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            r = self.client.get(robots_url)
            if r.status_code == 200 and not self._is_challenge(r.text):
                state.robots = Protego.parse(r.text)
            elif r.status_code in (401, 403) or self._is_challenge(r.text):
                # Unreadable robots behind a wall: treat the whole host as off limits.
                state.robots = Protego.parse("User-agent: *\nDisallow: /")
        except (TransportError, subprocess.TimeoutExpired, OSError):
            state.robots = None  # network failure on robots: fall back to allow, as RFC 9309 permits

    def allowed(self, url: str) -> bool:
        _, state = self._host(url)
        self._load_robots(url, state)
        if state.robots is None:
            return True
        return state.robots.can_fetch(url, self.user_agent)

    # ---- cache --------------------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json.gz")

    def _cache_read(self, url: str) -> dict | None:
        p = self._cache_path(url)
        if not p.exists():
            return None
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    def _cache_write(self, url: str, resp: Response, text: str, fetched_at: str) -> None:
        entry = {
            "url": url,
            "etag": resp.headers.get("etag"),
            "last_modified": resp.headers.get("last-modified"),
            "fetched_at": fetched_at,
            "status": resp.status_code,
            "text": text,
        }
        with gzip.open(self._cache_path(url), "wt", encoding="utf-8") as fh:
            json.dump(entry, fh)

    # ---- fetch --------------------------------------------------------------------------
    def _is_challenge(self, text: str) -> bool:
        head = text[:4000]
        return any(m in head for m in self.cfg["challenge_markers"])

    def _pace(self, host: str, state: HostState) -> None:
        delay = self.cfg.get("per_host_delay", {}).get(host, self.cfg["default_delay_seconds"])
        if state.robots is not None:
            crawl_delay = state.robots.crawl_delay(self.user_agent)
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        wait = state.last_request + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        state.last_request = time.monotonic()

    def _record(self, res: FetchResult) -> FetchResult:
        _host, state = self._host(res.url)
        state.stats[res.outcome] = state.stats.get(res.outcome, 0) + 1
        self.log.append({"url": res.url, "status": res.status, "outcome": res.outcome})
        return res

    def get(self, url: str, *, max_age_hours: float | None = None, decode_gzip: bool = False) -> FetchResult:
        host, state = self._host(url)
        if state.strikes >= self.cfg["circuit_breaker_threshold"]:
            return self._record(FetchResult(url, 0, outcome="breaker_open"))
        if not self.allowed(url):
            return self._record(FetchResult(url, 0, outcome="robots_disallowed"))

        cached = self._cache_read(url)
        if cached and max_age_hours is not None:
            age = datetime.now(UTC) - datetime.fromisoformat(cached["fetched_at"])
            if age.total_seconds() < max_age_hours * 3600:
                return self._record(self._from_cache(cached))

        headers = {}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        last_error = ""
        for attempt in range(self.cfg["max_retries"] + 1):
            self._pace(host, state)
            try:
                resp = self.client.get(url, headers=headers)
            except (TransportError, subprocess.TimeoutExpired, OSError) as exc:
                last_error = repr(exc)
                time.sleep(self._backoff(attempt))
                continue
            fetched_at = now_iso()
            if resp.status_code == 304 and cached:
                res = self._from_cache(cached)
                res.outcome = "not_modified"
                return self._record(res)
            if decode_gzip and resp.content[:2] == b"\x1f\x8b":
                text = gzip.decompress(resp.content).decode("utf-8", "replace")
            else:
                text = resp.text
            if resp.status_code in (403, 429) or self._is_challenge(text):
                state.strikes += 1
                if resp.status_code == 429 and attempt < self.cfg["max_retries"]:
                    retry_after = resp.headers.get("retry-after", "")
                    time.sleep(float(retry_after) if retry_after.isdigit() else self._backoff(attempt))
                    continue
                return self._record(FetchResult(url, resp.status_code, outcome="blocked", fetched_at=fetched_at))
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(self._backoff(attempt))
                continue
            state.strikes = 0
            if resp.status_code != 200:
                return self._record(
                    FetchResult(
                        url, resp.status_code, outcome="error", fetched_at=fetched_at, error=f"HTTP {resp.status_code}"
                    )
                )
            self._cache_write(url, resp, text, fetched_at)
            return self._record(
                FetchResult(
                    url,
                    200,
                    text=text,
                    fetched_at=fetched_at,
                    content_hash=hashlib.sha256(text.encode()).hexdigest()[:16],
                )
            )
        if cached:
            res = self._from_cache(cached)
            res.outcome = "ok"
            res.error = f"stale cache used after: {last_error}"
            return self._record(res)
        return self._record(FetchResult(url, 0, outcome="error", error=last_error))

    def _from_cache(self, cached: dict) -> FetchResult:
        text = cached["text"]
        return FetchResult(
            cached["url"],
            cached.get("status", 200),
            text=text,
            fetched_at=cached["fetched_at"],
            from_cache=True,
            content_hash=hashlib.sha256(text.encode()).hexdigest()[:16],
        )

    def _backoff(self, attempt: int) -> float:
        return self.cfg["backoff_base_seconds"] * (2**attempt) + random.uniform(0, 1)

    def stats(self) -> dict[str, dict[str, int]]:
        return {h: dict(s.stats) for h, s in self.hosts.items()}
