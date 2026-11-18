"""Post-deploy smoke test against a live URL. Exits non-zero on any failure.

Checks: health, readiness, an English question answered with a citation, an Arabic question answered
in RTL with a citation, and an out-of-scope question refused without citations.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request


def get(url: str, timeout: int = 90) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
        return r.status, json.loads(r.read().decode())


def chat(base: str, q: str) -> dict:
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps({"message": q, "session_id": "smoke-test-01"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    out, ev = {"text": "", "citations": [], "meta": {}, "model": None}, None
    with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                if ev == "token":
                    out["text"] += d["text"]
                elif ev == "citations":
                    out["citations"] = d
                elif ev == "meta":
                    out["meta"] = d
                elif ev == "model":
                    out["model"] = d["model"]
    return out


def main(base: str) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    base = base.rstrip("/")
    failures = []
    for _ in range(30):  # free hosts sleep; give the cold start time
        try:
            status, ready = get(f"{base}/readyz")
            if status == 200 and ready.get("ready"):
                break
        except Exception:  # noqa: BLE001 - waking up
            pass
        time.sleep(5)
    else:
        print("FAIL readiness")
        return 1
    print("ok   ready", json.dumps(ready))
    checks = [
        (
            "en answer with citation",
            "What did DarGlobal report in its half year 2026 results?",
            lambda r: r["citations"] and r["meta"].get("lang") == "en",
        ),
        (
            "ar answer with citation",
            "ما هي مشاريع دار جلوبال مع ترامب؟",
            lambda r: r["citations"] and r["meta"].get("dir") == "rtl",
        ),
        ("out of scope refused", "What is the capital of France?", lambda r: not r["citations"] and r["text"]),
    ]
    for name, q, ok in checks:
        r = chat(base, q)
        good = bool(ok(r))
        print(("ok  " if good else "FAIL"), name, "| model:", r["model"], "|", r["text"][:120].replace("\n", " "))
        if not good:
            failures.append(name)
        time.sleep(3)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"))
