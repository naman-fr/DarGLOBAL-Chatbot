"""Read side of the KB: hybrid retrieval (FTS5 BM25 + dense cosine, fused with RRF) and listing search."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from concierge import config

_TOKEN = re.compile(r"[\w؀-ۿ]+", re.UNICODE)


@dataclass
class Hit:
    chunk_id: str
    record_id: str
    kind: str
    brand: str
    lang: str
    title: str
    url: str
    fetched_at: str
    source_name: str
    text: str
    score: float = 0.0
    bm25_rank: int | None = None
    dense_rank: int | None = None
    dense_sim: float | None = None


@dataclass
class ListingFilters:
    purpose: str | None = None  # sale | rent | daily_rent
    property_type: str | None = None  # matched against property_type (case-insensitive substring)
    city: str | None = None
    district: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    min_bedrooms: int | None = None
    max_bedrooms: int | None = None
    min_area: float | None = None
    lang: str | None = None
    extra: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not any(
            [
                self.purpose,
                self.property_type,
                self.city,
                self.district,
                self.min_price,
                self.max_price,
                self.min_bedrooms,
                self.max_bedrooms,
                self.min_area,
            ]
        )


class KB:
    def __init__(self, kb_dir: str | Path | None = None) -> None:
        self.cfg = config.load("retrieval")
        self.dir = Path(kb_dir) if kb_dir else config.path(self.cfg["kb_dir"])
        self.db_path = self.dir / "kb.sqlite"
        self._local = threading.local()
        self.manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
        emb_path = self.dir / "embeddings.npy"
        self.vectors = np.load(emb_path, mmap_mode="r") if emb_path.exists() else None
        self._embedder = None
        self._embed_lock = threading.Lock()
        self.rowids = [r[0] for r in self._con().execute("SELECT rowid FROM chunks ORDER BY rowid")]

    # sqlite connections are per thread; FastAPI runs sync work in a pool
    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    @property
    def version(self) -> str:
        return self.manifest["kb_version"]

    def embedder(self):
        if self._embedder is None and self.vectors is not None:
            with self._embed_lock:
                if self._embedder is None:
                    from concierge.kb.embed import Embedder

                    self._embedder = Embedder()
        return self._embedder

    def embed_query(self, text: str) -> np.ndarray | None:
        model = self.embedder()
        if model is None:
            return None
        return model.embed([text])[0]

    # ---- retrieval -------------------------------------------------------------------------
    def _row_to_hit(self, row: sqlite3.Row) -> Hit:
        return Hit(
            chunk_id=row["id"],
            record_id=row["record_id"],
            kind=row["kind"],
            brand=row["brand"],
            lang=row["lang"],
            title=row["title"],
            url=row["url"],
            fetched_at=row["fetched_at"],
            source_name=row["source_name"],
            text=row["text"],
        )

    def bm25(self, query: str, n: int, brand: str | None = None) -> list[tuple[int, float]]:
        tokens = [t for t in _TOKEN.findall(query.lower()) if len(t) > 1]
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens[:24])
        sql = (
            "SELECT c.rowid, bm25(chunks_fts, 2.0, 1.0) AS s FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ?" + (" AND c.brand = ?" if brand else "") + " ORDER BY s LIMIT ?"
        )
        args = [match] + ([brand] if brand else []) + [n]
        return [(r[0], -r[1]) for r in self._con().execute(sql, args)]

    def dense(self, qvec: np.ndarray | None, n: int, brand: str | None = None) -> list[tuple[int, float]]:
        if qvec is None or self.vectors is None:
            return []
        sims = np.asarray(self.vectors, dtype=np.float32) @ qvec
        if brand:
            brands = self._brand_mask()
            sims = np.where(brands == brand, sims, -1.0)
        idx = np.argsort(-sims)[:n]
        return [(self.rowids[i], float(sims[i])) for i in idx if sims[i] > -1.0]

    def _brand_mask(self) -> np.ndarray:
        if not hasattr(self, "_brands"):
            self._brands = np.array([r[0] for r in self._con().execute("SELECT brand FROM chunks ORDER BY rowid")])
        return self._brands

    def search(
        self, query: str, k: int | None = None, brand: str | None = None, extra_queries: list[str] | None = None
    ) -> list[Hit]:
        k = k or self.cfg["top_k"]
        n = self.cfg["candidates_per_retriever"]
        rrf_k = self.cfg["rrf_k"]
        w = self.cfg["weights"]
        fused: dict[int, float] = {}
        bm_rank: dict[int, int] = {}
        dn_rank: dict[int, int] = {}
        dn_sim: dict[int, float] = {}
        for q in [query] + (extra_queries or []):
            for rank, (rowid, _) in enumerate(self.bm25(q, n, brand)):
                fused[rowid] = fused.get(rowid, 0.0) + w["bm25"] / (rrf_k + rank + 1)
                bm_rank.setdefault(rowid, rank)
        qvec = self.embed_query(query)
        for rank, (rowid, sim) in enumerate(self.dense(qvec, n, brand)):
            fused[rowid] = fused.get(rowid, 0.0) + w["dense"] / (rrf_k + rank + 1)
            dn_rank.setdefault(rowid, rank)
            dn_sim[rowid] = sim
        top = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        if not top:
            return []
        placeholders = ",".join("?" * len(top))
        rows = {
            r["rowid"]: r
            for r in self._con().execute(
                f"SELECT rowid, * FROM chunks WHERE rowid IN ({placeholders})", [t[0] for t in top]
            )
        }
        hits = []
        for rowid, score in top:
            h = self._row_to_hit(rows[rowid])
            h.score, h.bm25_rank, h.dense_rank, h.dense_sim = (
                score,
                bm_rank.get(rowid),
                dn_rank.get(rowid),
                dn_sim.get(rowid),
            )
            hits.append(h)
        return hits

    def is_confident(self, hits: list[Hit], on_topic: bool = True) -> bool:
        """Semantic closeness decides; lexical overlap alone is too easy to hit by accident
        ("Paris" matches a project called "Parisiana"). Questions with no brand or domain cue must
        clear a higher bar."""
        if not hits:
            return False
        key = "min_dense_similarity" if on_topic else "off_topic_min_similarity"
        best = max((h.dense_sim or 0.0) for h in hits[:3])
        if self.vectors is None:  # lexical-only mode
            return hits[0].bm25_rank is not None and on_topic
        return best >= self.cfg[key]

    # ---- structured listings -------------------------------------------------------------
    def mentions_known_entity(self, text: str) -> bool:
        """True when the question names something the KB has a page about (a project, an announcement)."""
        if not hasattr(self, "_entities"):
            names = set()
            for (title,) in self._con().execute("SELECT DISTINCT title FROM records WHERE kind != 'listing'"):
                name = re.split(r" — | \| ", title)[0].strip().lower()
                if len(name) >= 5 and len(name.split()) <= 6:
                    names.add(name)
            generic = set(self.cfg.get("generic_titles", []))
            self._entities = sorted(names - generic, key=len, reverse=True)
        low = text.lower()
        return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", low) for n in self._entities)

    def has_listings(self) -> bool:
        return self._con().execute("SELECT 1 FROM listings LIMIT 1").fetchone() is not None

    def listing_vocab(self) -> dict[str, list[str]]:
        con = self._con()
        cities = [r[0] for r in con.execute("SELECT DISTINCT city FROM listings WHERE city != '' ORDER BY city")]
        types = [r[0] for r in con.execute("SELECT DISTINCT property_type FROM listings WHERE property_type != ''")]
        districts = [r[0] for r in con.execute("SELECT DISTINCT district FROM listings WHERE district != ''")]
        return {"cities": cities, "types": types, "districts": districts}

    def search_listings(self, f: ListingFilters, limit: int | None = None) -> tuple[list[dict], int]:
        limit = limit or self.cfg["listing_results"]
        where, args = ["1=1"], []
        if f.purpose:
            where.append("purpose = ?")
            args.append(f.purpose)
        if f.property_type:
            where.append("LOWER(property_type) LIKE ?")
            args.append(f"%{f.property_type.lower()}%")
        if f.city:
            where.append("LOWER(city) = ?")
            args.append(f.city.lower())
        if f.district:
            where.append("LOWER(district) LIKE ?")
            args.append(f"%{f.district.lower()}%")
        if f.min_price is not None:
            where.append("price >= ?")
            args.append(f.min_price)
        if f.max_price is not None:
            where.append("price <= ?")
            args.append(f.max_price)
        if f.min_bedrooms is not None:
            where.append("bedrooms >= ?")
            args.append(f.min_bedrooms)
        if f.max_bedrooms is not None:
            where.append("bedrooms <= ?")
            args.append(f.max_bedrooms)
        if f.min_area is not None:
            where.append("area_sqm >= ?")
            args.append(f.min_area)
        if f.lang:
            where.append("lang = ?")
            args.append(f.lang)
        clause = " AND ".join(where)
        con = self._con()
        total = con.execute(f"SELECT COUNT(DISTINCT listing_id) FROM listings WHERE {clause}", args).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM listings WHERE {clause} GROUP BY listing_id "
            "ORDER BY (price IS NULL), updated_at DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
        return [dict(r) for r in rows], total

    def listing_stats(self, f: ListingFilters) -> dict:
        rows, total = self.search_listings(f, limit=10_000)
        prices = sorted(r["price"] for r in rows if r["price"])
        if not prices:
            return {"count": total}
        mid = prices[len(prices) // 2]
        return {"count": total, "min": prices[0], "median": mid, "max": prices[-1], "currency": rows[0]["currency"]}

    def suggestion_rows(self, lang: str, per_brand: int = 30) -> list[dict]:
        """Raw material for suggested questions: distinct record titles per brand in the UI language,
        plus the most populated (type, purpose, city) listing combinations."""
        con = self._con()
        out: list[dict] = []
        for brand in config.load("sources")["brands"]:
            rows = con.execute(
                "SELECT brand, kind, MIN(lang) AS lang, title, json_extract(body, '$.section') AS section FROM records WHERE brand = ? AND lang = ? "
                "AND kind IN ('faq','article','corporate') AND LENGTH(title) BETWEEN 8 AND 90 "
                "GROUP BY title ORDER BY RANDOM() LIMIT ?",
                (brand, lang, per_brand),
            ).fetchall()
            out += [
                {**dict(r), "title": r["title"].split(" — ")[0] if r["kind"] != "faq" else r["title"]} for r in rows
            ]
        listing_brand = config.load("router")["listing_brand"]
        L = config.locale(lang)
        for r in con.execute(
            "SELECT property_type, purpose, city, COUNT(*) AS n FROM listings WHERE lang = ? AND property_type != '' "
            "AND city != '' GROUP BY 1,2,3 HAVING n >= 3 ORDER BY n DESC LIMIT 12",
            (lang,),
        ):
            out.append(
                {
                    "brand": listing_brand,
                    "kind": "listing",
                    "title": "",
                    "type": r["property_type"].lower(),
                    "purpose": L.get(f"purpose_{r['purpose']}", r["purpose"]),
                    "city": r["city"],
                }
            )
        return out
