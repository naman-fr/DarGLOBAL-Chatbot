"""Build the KB snapshot: SQLite entities + chunk FTS5 index + dense embeddings + manifest."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from concierge import config
from concierge.kb.chunk import chunk_record

SCHEMA = """
CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT, brand TEXT, lang TEXT, title TEXT, url TEXT,
                      fetched_at TEXT, source_name TEXT, body TEXT);
CREATE TABLE listings (id TEXT PRIMARY KEY, listing_id INTEGER, lang TEXT, title TEXT, purpose TEXT,
                       property_type TEXT, main_type TEXT, price REAL, currency TEXT, rent_frequency TEXT,
                       city TEXT, district TEXT, region_zone TEXT, bedrooms INTEGER, bathrooms INTEGER,
                       area_sqm REAL, rega_ad_license TEXT, is_rega_verified INTEGER, updated_at TEXT,
                       description TEXT, url TEXT, fetched_at TEXT);
CREATE INDEX listings_filter ON listings (purpose, city, property_type, price);
CREATE TABLE chunks (rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, record_id TEXT, kind TEXT, brand TEXT,
                     lang TEXT, title TEXT, url TEXT, fetched_at TEXT, source_name TEXT, text TEXT);
CREATE VIRTUAL TABLE chunks_fts USING fts5(title, text, content='chunks', content_rowid='rowid',
                                           tokenize='unicode61 remove_diacritics 2');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

LISTING_COLS = [
    "id",
    "listing_id",
    "lang",
    "title",
    "purpose",
    "property_type",
    "main_type",
    "price",
    "currency",
    "rent_frequency",
    "city",
    "district",
    "region_zone",
    "bedrooms",
    "bathrooms",
    "area_sqm",
    "rega_ad_license",
    "is_rega_verified",
    "updated_at",
    "description",
    "url",
    "fetched_at",
]


def load_records(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def kb_version(records: list[dict]) -> str:
    digest = hashlib.sha256()
    for r in sorted(records, key=lambda r: r["id"]):
        stable = {**r, "prov": {k: v for k, v in r["prov"].items() if k != "fetched_at"}}
        digest.update(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode())
    return f"{datetime.now(UTC):%Y%m%d}-{digest.hexdigest()[:8]}"


def embed_texts(texts: list[str], cfg: dict) -> np.ndarray:
    from concierge.kb.embed import Embedder, prepare_model

    return Embedder(prepare_model(cfg)).embed(texts, batch_size=cfg["batch_size"]).astype(np.float16)


def build(out_dir: str | None = None) -> dict:
    cfg = config.load("retrieval")
    out = config.path(out_dir or cfg["kb_dir"])
    out.mkdir(parents=True, exist_ok=True)
    records = load_records(config.path(cfg["records_path"]))
    version = kb_version(records)
    started = time.monotonic()

    db_path = out / "kb.sqlite"
    tmp_path = out / "kb.sqlite.tmp"
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    con.executescript(SCHEMA)

    chunks: list[dict] = []
    for r in records:
        p = r["prov"]
        con.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r["id"],
                r["kind"],
                r["brand"],
                p["language"],
                r["title"],
                p["source_url"],
                p["fetched_at"],
                p.get("source_name", ""),
                json.dumps(r, ensure_ascii=False),
            ),
        )
        if r["kind"] == "listing":
            row = {**r, "lang": p["language"], "fetched_at": p["fetched_at"]}
            row["is_rega_verified"] = int(bool(r.get("is_rega_verified")))
            con.execute(
                f"INSERT INTO listings VALUES ({','.join('?' * len(LISTING_COLS))})", [row.get(c) for c in LISTING_COLS]
            )
        if r["kind"] in cfg["index_kinds"]:
            chunks.extend(chunk_record(r, cfg["chunk_chars"], cfg["chunk_overlap"]))

    for i, c in enumerate(chunks, start=1):
        con.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                c["id"],
                c["record_id"],
                c["kind"],
                c["brand"],
                c["lang"],
                c["title"],
                c["url"],
                c["fetched_at"],
                c["source_name"],
                c["text"],
            ),
        )
    con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")

    emb_cfg = cfg["embedding"]
    dim = 0
    if emb_cfg["enabled"] and chunks:
        vecs = embed_texts([f"{c['title']}\n{c['text']}" for c in chunks], emb_cfg)
        tmp_vec = out / "embeddings.tmp.npy"
        np.save(tmp_vec, vecs)
        tmp_vec.replace(out / "embeddings.npy")  # swap atomically so a running reader never sees half a file
        dim = int(vecs.shape[1])

    manifest = {
        "kb_version": version,
        "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "records": len(records),
        "records_by_brand_kind": dict(Counter(f"{r['brand']}/{r['kind']}" for r in records)),
        "chunks": len(chunks),
        "chunks_by_brand_lang": dict(Counter(f"{c['brand']}/{c['lang']}" for c in chunks)),
        "embedding_model": emb_cfg["model"] if emb_cfg["enabled"] else None,
        "embedding_dim": dim,
        "chunk_chars": cfg["chunk_chars"],
        "chunk_overlap": cfg["chunk_overlap"],
        "last_fetch": max((r["prov"]["fetched_at"] for r in records), default=None),
        "build_seconds": round(time.monotonic() - started, 1),
    }
    for k, v in manifest.items():
        con.execute("INSERT INTO meta VALUES (?,?)", (k, json.dumps(v)))
    con.commit()
    con.execute("VACUUM")
    con.close()
    tmp_path.replace(db_path)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report = config.path(cfg["records_path"]).with_name("crawl_report.json")
    if report.exists():
        (out / "crawl_report.json").write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(*sys.argv[1:]), indent=2))
