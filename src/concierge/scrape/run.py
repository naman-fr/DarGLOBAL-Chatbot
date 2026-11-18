"""Run every spider, validate records, quarantine failures, write the crawl report."""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from concierge import config
from concierge.scrape import darglobal, wasalt
from concierge.scrape.http import PoliteClient, now_iso
from concierge.scrape.schemas import MODELS


def validate(raw: dict) -> tuple[dict | None, str | None]:
    model = MODELS.get(raw.get("kind", ""))
    if model is None:
        return None, f"unknown kind {raw.get('kind')!r}"
    try:
        return model.model_validate(raw).model_dump(mode="json"), None
    except ValidationError as exc:
        return None, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def main(out_dir: str = "data/processed") -> int:
    started = time.monotonic()
    sources = config.load("sources")
    client = PoliteClient()
    out = config.path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(f"[ingest] {msg}", flush=True)

    records: dict[str, dict] = {}
    quarantine: list[dict] = []
    spiders = [
        ("darglobal", darglobal.crawl, sources["brands"]["darglobal"]),
        ("wasalt", wasalt.crawl, sources["brands"]["wasalt"]),
    ]
    for name, crawl, cfg in spiders:
        log(f"start {name}")
        try:
            for raw in crawl(client, cfg, log):
                rec, err = validate(raw)
                if err:
                    quarantine.append(
                        {
                            "id": raw.get("id"),
                            "kind": raw.get("kind"),
                            "reason": err,
                            "source_url": (raw.get("prov") or {}).get("source_url"),
                        }
                    )
                    continue
                records[rec["id"]] = rec  # idempotent upsert on stable ids
        except Exception as exc:  # a broken spider must not lose the other spider's records
            log(f"{name} aborted: {exc!r}")
            quarantine.append({"id": None, "kind": "spider", "reason": f"{name} aborted: {exc!r}"})
        log(f"done {name}: {sum(1 for r in records.values() if r['brand'] == name)} records")

    write_jsonl(out / "records.jsonl", records.values())
    write_jsonl(out / "quarantine.jsonl", quarantine)

    outcomes = Counter(e["outcome"] for e in client.log)
    report = {
        "finished_at": now_iso(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "user_agent": client.user_agent,
        "requests": len(client.log),
        "pages_by_outcome": dict(outcomes),
        "per_host": client.stats(),
        "records_by_brand_kind": dict(Counter(f"{r['brand']}/{r['kind']}" for r in records.values())),
        "records_by_language": dict(Counter(r["prov"]["language"] for r in records.values())),
        "quarantined": len(quarantine),
        "skipped_robots": [e["url"] for e in client.log if e["outcome"] == "robots_disallowed"],
        "blocked": sorted({e["url"] for e in client.log if e["outcome"] in ("blocked", "breaker_open")}),
    }
    (out / "crawl_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log(json.dumps({k: report[k] for k in ("requests", "pages_by_outcome", "records_by_brand_kind", "quarantined")}))
    return 0 if records else 1


def write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
